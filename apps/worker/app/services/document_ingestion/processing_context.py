from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy import select

from shared.core.config import settings
from shared.core.database_sync import get_sync_db_context
from shared.core.exceptions.domain_exceptions import (
    NotFoundException,
    ValidationException,
)
from shared.models.database.job import Job
from shared.services.redis.redis_sync_service import (
    SyncJobInfoRedisService,
    SyncJobMetadataService,
)
from shared.services.storage.job_file_storage import JobFileStorage


@dataclass(frozen=True)
class ParseJobContext:
    job_metadata: dict[str, object]
    job_user_id: str | None
    metadata_service: SyncJobMetadataService
    redis_service: Any
    s3_key: str


def load_parse_job_context(
    job_id: str,
    requested_user_id: str | None,
    redis_service: Any,
) -> ParseJobContext:
    job_info_service = SyncJobInfoRedisService(redis_service)
    job_info = job_info_service.get_job_info(job_id)

    if not job_info:
        logger.warning(
            f"JobInfo not found in Redis for job_id={job_id}; falling back to database"
        )
        with get_sync_db_context() as fallback_db:
            job_row = fallback_db.execute(
                select(Job).where(Job.job_id == job_id)
            ).scalar_one_or_none()

        if not job_row or not job_row.s3_key:
            raise NotFoundException(
                resource="JobInfo",
                resource_id=job_id,
                internal_message="job info not found in Redis or database",
            )

        s3_key: str = job_row.s3_key
        job_user_id: str | None = (
            str(job_row.user_id) if job_row.user_id else requested_user_id
        )
        logger.info(f"Recovered JobInfo from database: job_id={job_id}, s3_key={s3_key}")
    else:
        raw_s3_key = job_info.get("s3_key")
        if not isinstance(raw_s3_key, str) or not raw_s3_key:
            raise NotFoundException(
                resource="JobInfo",
                resource_id="s3_key",
                internal_message="Missing s3_key in job_info",
            )

        s3_key = raw_s3_key
        raw_job_user_id = job_info.get("user_id")
        job_user_id = (
            raw_job_user_id if isinstance(raw_job_user_id, str) else requested_user_id
        )

    metadata_service = SyncJobMetadataService(redis_service)
    raw_job_metadata = metadata_service.get_metadata(job_id)
    if not isinstance(raw_job_metadata, dict) or not raw_job_metadata:
        raise NotFoundException(
            resource="JobMetadata",
            resource_id=job_id,
            internal_message=f"Job metadata not found for job_id={job_id}",
        )

    return ParseJobContext(
        job_metadata=dict(raw_job_metadata),
        job_user_id=job_user_id,
        metadata_service=metadata_service,
        redis_service=redis_service,
        s3_key=s3_key,
    )


def assert_source_file_within_size_limit(s3_key: str) -> None:
    file_info = JobFileStorage().verify_upload_exists(s3_key)
    if not file_info.get("exists"):
        raise NotFoundException(
            resource="S3File",
            resource_id=s3_key,
            internal_message=f"S3 file not found: {s3_key}",
        )

    logger.info(f"S3 file verified: {s3_key}")

    file_size = file_info.get("size", 0)
    file_extension = os.path.splitext(s3_key)[1].lower()
    if file_size > settings.MAX_FILE_SIZE:
        limit_mb = settings.MAX_FILE_SIZE // (1024 * 1024)
        raise ValidationException(
            user_message=f"File size exceeds limit (max {limit_mb}MB for {file_extension})",
            violations=[
                {
                    "field": "file_size",
                    "description": (
                        f"Size {file_size} bytes exceeds limit of "
                        f"{settings.MAX_FILE_SIZE} bytes"
                    ),
                }
            ],
        )
