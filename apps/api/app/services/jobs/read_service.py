from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.repositories.job_repository import JobRepository
from app.services.jobs.result_projection import (
    build_job_result_response,
    to_job_status_value,
)
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import (
    JobOperationException,
    NotFoundException,
    PermissionDeniedException,
    ValidationException,
)
from shared.models.schemas.job import JobList, JobResultResponse
from shared.services.redis import RedisServiceFactory
from shared.utils.utc_now import utc_now_naive


def check_job_permission(job, user_id: str, job_id: str) -> None:
    if not job:
        raise NotFoundException(
            resource="Job", resource_id=job_id, internal_message="Job not found"
        )

    if str(job.user_id) != user_id:
        raise PermissionDeniedException(
            user_message="You don't have permission to access this job",
        )


def normalize_naive_utc_filter_datetime(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


async def list_jobs_for_user(
    db: AsyncSession,
    *,
    user_id: str,
    page: int,
    page_size: int,
    job_status: Optional[str],
    job_type: Optional[str],
    recent_days: Optional[int],
    start_time: Optional[datetime],
    end_time: Optional[datetime],
) -> JobList:
    try:
        job_repo = JobRepository()

        if recent_days not in (None, 1, 7, 30):
            raise ValidationException(
                user_message="recent_days only supports 1, 7, or 30",
                violations=[{"field": "recent_days", "description": "Invalid value"}],
            )

        created_after: Optional[datetime] = None
        if recent_days:
            created_after = utc_now_naive() - timedelta(days=recent_days)

        normalized_start_time = normalize_naive_utc_filter_datetime(start_time)
        normalized_end_time = normalize_naive_utc_filter_datetime(end_time)

        if (
            normalized_start_time
            and normalized_end_time
            and normalized_start_time > normalized_end_time
        ):
            raise ValidationException(
                user_message="start_time cannot be later than end_time",
                violations=[
                    {"field": "start_time", "description": "Must be before end_time"}
                ],
            )

        if normalized_start_time:
            created_after = normalized_start_time
        created_before = normalized_end_time

        total_count = await job_repo.count_jobs_by_user(
            db=db,
            user_id=user_id,
            created_after=created_after,
            created_before=created_before,
            job_type=job_type,
            job_status=job_status,
        )
        jobs = await job_repo.get_jobs_by_user(
            db=db,
            user_id=user_id,
            limit=page_size,
            offset=(page - 1) * page_size,
            created_after=created_after,
            created_before=created_before,
            job_type=job_type,
            job_status=job_status,
        )

        redis_service = RedisServiceFactory.get_service()
        job_responses = []
        for job in jobs:
            job_metadata = await job_repo.get_job_metadata(
                db, job.job_id, redis_service
            )
            job_responses.append(
                await build_job_result_response(
                    job=job,
                    job_metadata=job_metadata,
                    progress=None,
                )
            )

        total_pages = math.ceil(total_count / page_size) if total_count > 0 else 0
        return JobList(
            jobs=job_responses,
            total=total_count,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    except ValidationException:
        raise
    except Exception as exc:
        logger.error(f"Failed to list jobs: {exc}")
        raise JobOperationException(
            internal_message=f"Failed to get job list: {str(exc)}"
        )


async def get_job_result_for_user(
    db: AsyncSession,
    *,
    job_id: str,
    user_id: str,
) -> JobResultResponse:
    try:
        job_repo = JobRepository()
        job = await job_repo.get_job_by_id(db, job_id)
        check_job_permission(job, user_id, job_id)
        assert job is not None

        progress = None
        if to_job_status_value(job.status) == "running":
            progress = {"total_pages": 10, "processed_pages": 5}

        redis_service = RedisServiceFactory.get_service()
        job_metadata = await job_repo.get_job_metadata(db, job_id, redis_service)
        return await build_job_result_response(
            job=job,
            job_metadata=job_metadata,
            progress=progress,
        )

    except NotFoundException:
        raise
    except PermissionDeniedException:
        raise
    except Exception as exc:
        logger.error(f"Failed to get job result: {exc}")
        raise JobOperationException(
            internal_message=f"Failed to get job result: {str(exc)}"
        )
