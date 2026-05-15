"""
Unified Jobs API routes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.services.job_creation_service import create_job_from_request
from app.services.job_read_service import (
    get_job_result_for_user,
    list_jobs_for_user,
)
from app.services.rate_limit.dependencies import (
    CurrentUser,
    enforce_job_creation_capacity,
    require_billing_limits,
    with_current_user,
)
from app.services.job_upload_confirmation_service import confirm_job_upload
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db
from shared.models.schemas.job import (
    ConfirmUploadRequest,
    JobCreate,
    JobList,
    JobResponse,
    JobResultResponse,
)

router = APIRouter(tags=["Jobs"])


# ==================== Shared Helpers ====================


@router.post("", response_model=JobResponse, summary="Create a parsing job")
@router.post("/", include_in_schema=False)
async def create_job(  # pyright: ignore[reportGeneralTypeIssues]
    payload: JobCreate,
    http_request: Request,
    current_user: CurrentUser = Depends(require_billing_limits),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a parsing job.
    """
    return await create_job_from_request(
        db,
        payload=payload,
        current_user=current_user,
        enforce_capacity=enforce_job_creation_capacity,
        request=http_request,
    )


@router.get("", response_model=JobList, summary="List jobs")
@router.get("/page", response_model=JobList, include_in_schema=False)
async def list_jobs(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    job_status: Optional[str] = Query(None, description="Status filter"),
    job_type: Optional[str] = Query(None, description="Job type filter"),
    recent_days: Optional[int] = Query(
        None,
        description="Recent-day filter; supported values are 1, 7, and 30",
        enum=[1, 7, 30],
    ),
    start_time: Optional[datetime] = Query(
        None, description="Start time in ISO format"
    ),
    end_time: Optional[datetime] = Query(None, description="End time in ISO format"),
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List jobs for the current user.
    """
    return await list_jobs_for_user(
        db,
        user_id=current_user.user_id,
        page=page,
        page_size=page_size,
        job_status=job_status,
        job_type=job_type,
        recent_days=recent_days,
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/{job_id}", response_model=JobResultResponse, summary="Get a job result")
async def get_job_result(
    job_id: str,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Return the result payload for one job.
    """
    return await get_job_result_for_user(
        db,
        job_id=job_id,
        user_id=current_user.user_id,
    )


@router.post(
    "/{job_id}/confirm-upload",
    response_model=dict,
    summary="Confirm file upload",
)
async def confirm_upload(
    job_id: str,
    request: Optional[ConfirmUploadRequest] = None,
    current_user: CurrentUser = Depends(with_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Confirm a completed file upload as a fallback path.
    """
    return await confirm_job_upload(
        db,
        job_id=job_id,
        request=request,
        user_id=current_user.user_id,
    )
