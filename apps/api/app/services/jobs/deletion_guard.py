"""Protect shared originals and revision assets from job deletion requests."""

from typing import NoReturn

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import (
    ConflictException,
    NotFoundException,
)
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult


async def reject_job_deletion(
    db: AsyncSession, *, user_id: str, job_id: str
) -> NoReturn:
    job: Job | None = (
        await db.execute(
            select(Job).where(Job.job_id == job_id, Job.user_id == user_id)
        )
    ).scalar_one_or_none()
    if job is None:
        raise NotFoundException(resource="Job", resource_id=job_id)
    revision: JobResult | None = (
        await db.execute(select(JobResult).where(JobResult.job_id == job_id))
    ).scalar_one_or_none()
    if revision is not None and revision.demo_document_id is not None:
        raise ConflictException(
            user_message="This job owns a retained shared demo revision. Its original and assets cannot be deleted; archive the demo document instead.",
            resource="Demo revision",
            resource_id=revision.id,
        )
    # This checkout retains processing jobs and has no deletion operation.
    raise HTTPException(
        status_code=405,
        detail="Jobs are retained processing records. Use document archive to remove content from retrieval.",
    )
