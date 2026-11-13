"""Renew a waiting job's source upload capability without changing its claim."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import (
    ConflictException,
    NotFoundException,
)
from shared.models.database.job import Job
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.services.jobs.source_upload_deadline import get_remaining_source_upload_seconds
from shared.services.retrieval.demo_job_scope import validate_job_corpus
from shared.services.storage.file_upload_service import FileUploadService
from shared.services.storage.job_file_storage import JobFileStorage


async def renew_source_upload(
    db: AsyncSession, *, user_id: str, job_id: str
) -> dict[str, Any]:
    job: Job | None = (
        await db.execute(
            select(Job).where(Job.job_id == job_id, Job.user_id == user_id)
        )
    ).scalar_one_or_none()
    if job is None:
        raise NotFoundException(resource="Job", resource_id=job_id)
    await db.run_sync(lambda session: validate_job_corpus(session, job=job))
    if job.status != "waiting-file" or not job.s3_key:
        raise ConflictException(
            user_message="Only waiting-file jobs can renew source upload URLs.",
            resource="Job",
            resource_id=job_id,
        )
    extension: str = "." + job.s3_key.rsplit(".", 1)[-1] if "." in job.s3_key else ""
    source_integrity = JobMetadataHelper.get_source_integrity(job.job_metadata)
    if source_integrity is not None:
        remaining = get_remaining_source_upload_seconds(job.created_at)
        if remaining == 0:
            raise ConflictException(
                user_message="The source upload has expired.",
                reason="ABORTED",
                resource="Job",
                resource_id=job_id,
            )
        return await FileUploadService().generate_verified_upload_url(
            storage_key=job.s3_key,
            file_extension=extension,
            source_integrity=source_integrity,
            expiration=remaining,
        )
    return await asyncio.to_thread(
        JobFileStorage().generate_upload_url, job_id=job_id, file_extension=extension
    )
