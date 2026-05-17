from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.services.jobs.job_read_model import JobReadModel
from app.services.jobs.job_read_model import check_job_permission
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.schemas.job import JobList, JobResultResponse

__all__ = [
    "check_job_permission",
    "get_job_result_for_user",
    "list_jobs_for_user",
]


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
    return await JobReadModel().list_jobs_for_user(
        db,
        user_id=user_id,
        page=page,
        page_size=page_size,
        job_status=job_status,
        job_type=job_type,
        recent_days=recent_days,
        start_time=start_time,
        end_time=end_time,
    )


async def get_job_result_for_user(
    db: AsyncSession,
    *,
    job_id: str,
    user_id: str,
) -> JobResultResponse:
    return await JobReadModel().get_job_result_for_user(
        db,
        job_id=job_id,
        user_id=user_id,
    )
