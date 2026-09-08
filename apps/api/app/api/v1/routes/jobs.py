"""
Unified Jobs API routes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.api.dependencies.auth import require_write_permission
from app.api.dependencies.current_user import with_current_user
from app.api.dependencies.job_admission import require_billing_limits
from app.services.document_ingestion import DocumentIngestionService
from app.services.jobs import (
    delete_job_for_user,
    get_job_result_for_user,
    list_jobs_for_user,
)
from app.services.rate_limit.data_structures import CurrentUser
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db
from shared.models.schemas.job import (
    ConfirmUploadRequest,
    JobCreate,
    JobDeleteResponse,
    JobList,
    JobResponse,
    JobResultResponse,
)

router = APIRouter(tags=["Jobs"])
_document_ingestion_service = DocumentIngestionService()


# ==================== Shared Helpers ====================


@router.post("", response_model=JobResponse, summary="Create a parsing job")
@router.post("/", include_in_schema=False)
async def create_job(  # pyright: ignore[reportGeneralTypeIssues]
    payload: JobCreate,
    current_user: CurrentUser = Depends(require_billing_limits),
    _write_permission: None = Depends(require_write_permission),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a parsing job.
    """
    return await _document_ingestion_service.create_v1_job(
        db,
        payload=payload,
        current_user=current_user,
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
    namespace: Optional[str] = Query(None, description="Namespace filter"),
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
        namespace=namespace,
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


@router.delete(
    "/{job_id}", response_model=JobDeleteResponse, summary="Delete a job"
)
async def delete_job(
    job_id: str,
    archive_document: bool = Query(
        True,
        description=(
            "Archive the document this job produced, removing it from "
            "retrieval. Skipped when another live job still targets it."
        ),
    ),
    current_user: CurrentUser = Depends(with_current_user),
    _write_permission: None = Depends(require_write_permission),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a job.

    The job is soft-deleted: its row is retained so in-flight workers, billing
    records, and audit history stay intact, but it no longer appears in the
    job read APIs.
    """
    return await delete_job_for_user(
        db,
        job_id=job_id,
        user_id=current_user.user_id,
        archive_document=archive_document,
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
    _write_permission: None = Depends(require_write_permission),
    db: AsyncSession = Depends(get_db),
):
    """
    Confirm a completed file upload as a fallback path.
    """
    return await _document_ingestion_service.confirm_upload(
        db,
        job_id=job_id,
        request_payload=request,
        user_id=current_user.user_id,
    )
