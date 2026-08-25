from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.core.database_sync import get_sync_db_context
from shared.core.exceptions.domain_exceptions import (
    NotFoundException,
)
from shared.models.database.job import Job
from shared.services.redis.redis_sync_service import (
    SyncJobInfoRedisService,
    SyncJobMetadataService,
)


@dataclass(frozen=True)
class ParseJobContext:
    job_metadata: dict[str, object]
    job_user_id: str | None
    metadata_service: SyncJobMetadataService
    redis_service: Any
    s3_key: str


def persist_job_metadata_updates(
    *,
    job_id: str,
    job_context: ParseJobContext,
    metadata_updates: dict[str, object],
) -> None:
    """Merge metadata into Redis and the durable Job row.

    Redis remains the live working copy during processing. The Job row is
    the durable record after Redis TTL expires, so token usage and other
    stage fields must be written here as well.
    """
    if not metadata_updates:
        return
    with get_sync_db_context() as db:
        job = _select_job_row_for_update(db, job_id)
        if job is not None:
            job.job_metadata = {
                **dict(job.job_metadata or {}),
                **metadata_updates,
            }
    job_context.metadata_service.update_metadata(job_id, metadata_updates)
    job_context.job_metadata.update(metadata_updates)


def load_parse_job_context(
    job_id: str,
    requested_user_id: str | None,
    redis_service: Any,
) -> ParseJobContext:
    job_info_service = SyncJobInfoRedisService(redis_service)
    job_info = job_info_service.get_job_info(job_id)
    job_row: Job | None = None

    if not job_info:
        logger.warning(
            f"JobInfo not found in Redis for job_id={job_id}; falling back to database"
        )
        job_row = _load_job_row(job_id)

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
        job_info_service.save_job_info(
            job_id,
            {
                "job_id": job_id,
                "s3_key": s3_key,
                "user_id": job_user_id,
                "webhook_enabled": bool(job_row.webhook_enabled),
                "job_type": "document_ingestion",
                "source_type": job_row.source_type,
            },
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
        job_row = job_row or _load_job_row(job_id)
        raw_job_metadata = job_row.job_metadata if job_row else None
        if isinstance(raw_job_metadata, dict) and raw_job_metadata:
            metadata_service.save_metadata(job_id, raw_job_metadata)
            logger.info(f"Recovered JobMetadata from database: job_id={job_id}")
        else:
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


def _load_job_row(job_id: str) -> Job | None:
    with get_sync_db_context() as fallback_db:
        return _select_job_row(fallback_db, job_id)


def _select_job_row(db: Session, job_id: str) -> Job | None:
    return db.execute(select(Job).where(Job.job_id == job_id)).scalar_one_or_none()


def _select_job_row_for_update(db: Session, job_id: str) -> Job | None:
    return db.execute(
        select(Job).where(Job.job_id == job_id).with_for_update()
    ).scalar_one_or_none()
