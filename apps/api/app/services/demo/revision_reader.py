"""Resolve only complete demo revisions; never serve staged parser payloads."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from shared.core.exceptions.demo_not_ready import DemoNotReadyException
from shared.core.exceptions.domain_exceptions import NotFoundException
from shared.models.database.demo_corpus import DemoDocument, DemoDocumentMapUnitIndex
from shared.models.database.job_result import JobResult


async def resolve_demo_revision(
    db: AsyncSession, *, document_id: str, job_result_id: str | None = None
) -> tuple[DemoDocument, JobResult]:
    document: DemoDocument | None = (
        await db.execute(
            select(DemoDocument).where(
                DemoDocument.document_id == document_id, DemoDocument.status == "active"
            )
        )
    ).scalar_one_or_none()
    if document is None:
        raise NotFoundException(resource="Demo document", resource_id=document_id)
    revisionId: str | None = job_result_id or document.current_job_result_id
    if revisionId is None:
        raise DemoNotReadyException(is_explicit_source=True)
    result: JobResult | None = (
        await db.execute(
            select(JobResult)
            .options(noload(JobResult.chunks), noload(JobResult.job))
            .join(
                DemoDocumentMapUnitIndex,
                (DemoDocumentMapUnitIndex.job_result_id == JobResult.id)
                & (DemoDocumentMapUnitIndex.document_id == document_id),
            )
            .where(
                JobResult.id == revisionId, JobResult.demo_document_id == document_id
            )
        )
    ).scalar_one_or_none()
    if result is None:
        raise NotFoundException(resource="Demo revision", resource_id=revisionId)
    return document, result
