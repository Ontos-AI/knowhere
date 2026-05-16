"""
Document-scope rules used by document-ingestion workflows.
"""

from __future__ import annotations

import uuid
from typing import Optional

from app.repositories.document_repository import DocumentRepository
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import (
    ConflictException,
    NotFoundException,
    ValidationException,
)
from shared.models.database.job import Job

_ACTIVE_JOB_STATUSES = ("waiting-file", "pending", "running", "converting")


def build_active_job_for_document_query(*, user_id: str, document_id: str):
    return (
        select(Job)
        .where(Job.user_id == user_id)
        .where(Job.status.in_(_ACTIVE_JOB_STATUSES))
        .where(Job.job_metadata["document_id"].as_string() == document_id)
        .order_by(Job.created_at.asc())
    )


async def find_active_job_for_document(
    db: AsyncSession,
    *,
    user_id: str,
    document_id: str,
) -> Job | None:
    result = await db.execute(
        build_active_job_for_document_query(
            user_id=user_id,
            document_id=document_id,
        )
    )
    return result.scalars().first()


def is_active_document_job_unique_violation(exc: Exception) -> bool:
    text = f"{exc} {getattr(exc, 'orig', '')}"
    return "uq_jobs_user_active_document" in text


def raise_document_ingestion_conflict(
    *,
    document_id: str,
    active_job_id: str | None = None,
) -> None:
    suffix = f" Active job: {active_job_id}." if active_job_id else ""
    raise ConflictException(
        user_message=f"Document already has an active ingestion job.{suffix}",
        reason="ABORTED",
        resource="Document",
        resource_id=document_id,
        internal_message=f"Active ingestion conflict for document_id={document_id}",
    )


async def resolve_effective_document_scope(
    db: AsyncSession,
    *,
    user_id: str,
    document_id: Optional[str],
    requested_namespace: Optional[str],
    repository: DocumentRepository | None = None,
) -> tuple[str, str]:
    if not document_id:
        return f"doc_{uuid.uuid4().hex[:12]}", requested_namespace or "default"

    document = await (repository or DocumentRepository()).get_document(
        db,
        document_id=document_id,
        user_id=user_id,
    )
    if document is None or getattr(document, "status", None) == "archived":
        raise NotFoundException(
            resource="Document",
            resource_id=document_id,
            internal_message=f"Document not found for update flow: {document_id}",
        )
    if requested_namespace and requested_namespace != document.namespace:
        raise ValidationException(
            user_message="namespace must match the existing document namespace",
            violations=[
                {
                    "field": "namespace",
                    "description": "Does not match existing document namespace",
                }
            ],
        )
    return document.document_id, document.namespace
