"""Claim a stable shared source before creating its ordinary ingestion job."""

from __future__ import annotations

from uuid import uuid5, NAMESPACE_URL

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import (
    NotFoundException,
    ValidationException,
)
from shared.models.database.demo_corpus import DemoDocument
from shared.models.database.job import Job
from shared.models.schemas.job import JobCreateBase
from shared.services.retrieval.corpus_storage import DEMO_NAMESPACE, DEMO_OWNER
from shared.services.retrieval.demo_authorization import authorize_demo_transaction
from app.services.document_ingestion.scope_service import (
    raise_document_ingestion_conflict,
)


async def resolve_demo_scope(
    db: AsyncSession, *, user_id: str, payload: JobCreateBase
) -> str:
    await db.run_sync(
        lambda session: authorize_demo_transaction(session, user_id=user_id)
    )
    sourceId: str = str(payload.data_id or "").strip()
    if not sourceId or len(sourceId) > 128:
        raise ValidationException(
            user_message="Demo ingestion requires data_id with at most 128 characters.",
            violations=[{"field": "data_id", "description": "Required demo source ID"}],
        )
    # This transaction lock spans source creation and the JobRepository commit.
    # The partial unique index protects claims from any other API process too.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:source, 0))"),
        {"source": "demo:" + sourceId},
    )
    document = (
        await db.execute(
            select(DemoDocument)
            .where(DemoDocument.demo_source_id == sourceId)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if document is not None and document.status == "archived":
        raise NotFoundException(resource="Demo source", resource_id=sourceId)
    if payload.document_id and (
        document is None or payload.document_id != document.document_id
    ):
        raise ValidationException(
            user_message="document_id must belong to this demo data_id.",
            violations=[
                {"field": "document_id", "description": "Demo source mismatch"}
            ],
        )
    if document is None:
        fileName: str = str(payload.file_name or sourceId)
        document = DemoDocument(
            document_id="ddoc_" + uuid5(NAMESPACE_URL, sourceId).hex[:12],
            demo_source_id=sourceId,
            title=fileName,
            category_id="other",
            catalog_metadata={},
            user_id=DEMO_OWNER,
            namespace=DEMO_NAMESPACE,
            status="active",
            source_file_name=fileName,
            parse_track="chunk",
        )
        db.add(document)
        await db.flush()
    activeJob: Job | None = (
        (
            await db.execute(
                select(Job).where(
                    Job.job_metadata["corpus_target"].as_string() == "DEMO",
                    Job.job_metadata["demo_source_id"].as_string() == sourceId,
                    Job.status.in_(
                        ("waiting-file", "pending", "running", "converting")
                    ),
                )
            )
        )
        .scalars()
        .first()
    )
    if activeJob is not None:
        raise_document_ingestion_conflict(
            document_id=document.document_id, active_job_id=activeJob.job_id
        )
    return document.document_id
