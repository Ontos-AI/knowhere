"""
Unified Jobs API routes.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Literal, Optional, cast
from urllib.parse import urlparse

from app.repositories.job_repository import JobRepository
from app.services.job_document_scope_service import (
    find_active_job_for_document,
    is_active_document_job_unique_violation,
    raise_document_ingestion_conflict,
    resolve_effective_document_scope,
)
from app.services.knowledge.kb_orchestrator import KBOrchestrator
from app.services.rate_limit.dependencies import (
    CurrentUser,
    enforce_job_creation_capacity,
    require_billing_limits,
    with_current_user,
)
from app.services.state_machine import JobStateMachine
from fastapi import APIRouter, Depends, Query, Request
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.billing import MicroDollar
from shared.core.config import settings
from shared.core.database import get_db
from shared.core.exceptions.domain_exceptions import (
    ConflictException,
    JobOperationException,
    NotFoundException,
    PermissionDeniedException,
    RateLimitException,
    UnavailableException,
    ValidationException,
)
from shared.core.exceptions.webhook_exceptions import WebhookConfigException
from shared.core.state_machine.states import JobStatus
from shared.models.schemas.job import (
    ConfirmUploadRequest,
    JobCreate,
    JobList,
    JobResponse,
    JobResultResponse,
    StandardErrorObject,
)
from shared.services.storage.file_upload_service import FileUploadService
from shared.utils.utc_now import utc_now_naive
from shared.utils.url_security import (
    validate_http_url_and_resolve_ip_async,
)
from shared.utils.error_details import normalize_error_details
from shared.utils.url_file_type import resolve_file_extension_async

router = APIRouter(tags=["Jobs"])
JobStatusValue = Literal[
    "pending", "waiting-file", "running", "converting", "done", "failed"
]


# ==================== Shared Helpers ====================


def get_supported_formats() -> str:
    """Return the supported file extensions as a comma-separated string."""
    return ", ".join(sorted(settings.get_supported_extensions()))


async def transition_to_uploaded(
    db: AsyncSession,
    job_id: str,
    job_type: str,
    trigger: str = "manual_upload_completed",
):
    """
    Move the job into the uploaded flow.

    Args:
        db: Database session.
        job_id: Job identifier.
        job_type: Job type.
        trigger: Transition trigger.
    """
    state_machine = JobStateMachine()

    # Once the upload is confirmed, transition the job to pending.
    await state_machine.transition(
        db, job_id, JobStatus.PENDING.value, trigger, None, "system"
    )


async def start_workflow_for_job(
    db: AsyncSession,
    job_id: str,
    job_type: str,
    source_type: str,
    user_id: str,
    file_path: Optional[str] = None,
    file_url: Optional[str] = None,
):
    """
    Start the workflow for a job.

    Args:
        db: Database session.
        job_id: Job identifier.
        job_type: Job type.
        source_type: Source type.
        user_id: User identifier.
        file_path: File path.
        file_url: File URL.
    """
    if job_type == "kb_management":
        orchestrator = KBOrchestrator()
        await orchestrator.start_workflow(
            db=db,
            job_id=job_id,
            source_type=source_type,
            file_path=file_path,
            file_url=file_url,
            user_id=user_id,
        )
    else:
        raise ValidationException(
            user_message="Unsupported job type",
            violations=[
                {
                    "field": "job_type",
                    "description": f"Job type '{job_type}' is not supported",
                }
            ],
        )


def check_job_permission(job, user_id: str, job_id: str) -> None:
    """
    Verify that the job belongs to the current user.

    Args:
        job: Job object.
        user_id: Current user ID.
        job_id: Requested job ID.

    Raises:
        HTTPException: Raised when the user does not own the job.
    """
    if not job:
        raise NotFoundException(
            resource="Job", resource_id=job_id, internal_message="Job not found"
        )

    if str(job.user_id) != user_id:
        raise PermissionDeniedException(
            user_message="You don't have permission to access this job",
        )


def _build_error_response(
    job: Any, job_metadata: Optional[dict] = None
) -> Optional[StandardErrorObject]:
    """
    Build StandardErrorObject for embedded error pattern.

    Args:
        job: Job object with job_id, error_code, and error_message
        job_metadata: Job metadata dict that may contain error_details

    Returns:
        StandardErrorObject or None
    """
    if not job.error_message:
        return None

    # Extract error_details from job_metadata if present
    error_details = None
    if job_metadata and isinstance(job_metadata, dict):
        error_details = normalize_error_details(job_metadata.get("error_details"))

    return StandardErrorObject(
        code=job.error_code or "UNKNOWN",
        message=job.error_message,
        request_id=job.job_id,
        details=error_details,
    )


def create_job_response(
    job_id: str,
    job,
    source_type: str,
    data_id: Optional[str],
    namespace: Optional[str] = None,
    document_id: Optional[str] = None,
    upload_url: Optional[str] = None,
    upload_headers: Optional[dict] = None,
    expires_in: Optional[int] = None,
) -> JobResponse:
    """
    Build a JobResponse object.

    Args:
        job_id: Job identifier.
        job: Job object.
        source_type: Source type.
        data_id: Data identifier.
        upload_url: Upload URL in file mode.
        upload_headers: Upload headers in file mode.
        expires_in: Upload expiry in file mode.

    Returns:
        JobResponse: Serialized job response payload.
    """
    return JobResponse(
        job_id=job_id,
        status=job.status,
        source_type=source_type,
        data_id=data_id,
        namespace=namespace,
        document_id=document_id,
        created_at=job.created_at,
        upload_url=upload_url,
        upload_headers=upload_headers,
        expires_in=expires_in,
    )


def resolve_public_document_id(job) -> Optional[str]:
    """Expose document_id only after it is published in the persisted job result."""
    job_result = getattr(job, "job_result", None)
    published_document_id = getattr(job_result, "document_id", None)
    if isinstance(published_document_id, str) and published_document_id:
        return published_document_id

    return None


def validate_file_type(file_name: str) -> bool:
    """
    Return whether the file extension is supported.

    Args:
        file_name: File name.

    Returns:
        bool: Whether the file type is supported.
    """
    if not file_name:
        return False

    file_extension = os.path.splitext(file_name)[1].lower()

    return file_extension in settings.get_supported_extensions()


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a datetime to UTC."""
    if not dt:
        return None
    if dt.tzinfo:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def normalize_naive_utc_filter_datetime(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a query datetime into the naive UTC form used by database columns."""
    if dt is None:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def require_utc(dt: Optional[datetime], *, field_name: str) -> datetime:
    """Normalize a required datetime to UTC."""
    normalized_dt = ensure_utc(dt)
    if normalized_dt is None:
        raise JobOperationException(
            internal_message=f"Job is missing required datetime field: {field_name}"
        )
    return normalized_dt


def to_job_status_value(status: str) -> JobStatusValue:
    """Cast a persisted job status into the response literal type."""
    return cast(JobStatusValue, status)


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
    try:
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        # Validate input parameters.
        if payload.source_type == "file" and not payload.file_name:
            raise ValidationException(
                user_message="file_name is required when source_type is 'file'",
                violations=[
                    {
                        "field": "file_name",
                        "description": "Required for file source type",
                    }
                ],
            )
        if payload.source_type == "url" and not payload.source_url:
            raise ValidationException(
                user_message="source_url is required when source_type is 'url'",
                violations=[
                    {
                        "field": "source_url",
                        "description": "Required for url source type",
                    }
                ],
            )

        # Validate webhook config if present
        if payload.webhook:
            # Check for URL validity
            if payload.webhook.url:
                validation_result = await validate_http_url_and_resolve_ip_async(
                    payload.webhook.url,
                )
                if not validation_result.is_valid:
                    raise WebhookConfigException(
                        user_message="Invalid webhook URL",
                        internal_message=f"Webhook validation failed: {validation_result.error_message}",
                    )

        # Validate the source file type.
        if (
            payload.source_type == "file"
            and payload.file_name
            and not validate_file_type(payload.file_name)
        ):
            supported_formats = get_supported_formats()
            raise ValidationException(
                user_message=f"Unsupported file type. Supported formats: {supported_formats}",
                violations=[
                    {"field": "file_name", "description": "File type not supported"}
                ],
            )
        elif payload.source_type == "url":
            assert payload.source_url is not None
            # Resolve file type from URL path or Content-Type header
            file_ext = await resolve_file_extension_async(payload.source_url)
            if not file_ext:
                supported_formats = get_supported_formats()
                raise ValidationException(
                    user_message=f"Unsupported URL file type. Supported formats: {supported_formats}",
                    violations=[
                        {
                            "field": "source_url",
                            "description": "URL file type not supported",
                        }
                    ],
                )

        job_type = "kb_management"

        # Keep job creation lightweight.
        from shared.services.redis import RedisServiceFactory

        redis_service = RedisServiceFactory.get_service()

        # Build job_metadata without embedding user_config.
        from shared.models.schemas.job_metadata import JobMetadataHelper

        job_metadata = JobMetadataHelper.create_from_request(payload)
        requested_document_id = cast(Optional[str], job_metadata.get("document_id"))
        if requested_document_id:
            active_job = await find_active_job_for_document(
                db,
                user_id=current_user.user_id,
                document_id=requested_document_id,
            )
            if active_job is not None:
                raise_document_ingestion_conflict(
                    document_id=requested_document_id,
                    active_job_id=active_job.job_id,
                )
        (
            effective_document_id,
            effective_namespace,
        ) = await resolve_effective_document_scope(
            db,
            user_id=current_user.user_id,
            document_id=requested_document_id,
            requested_namespace=cast(Optional[str], payload.namespace),
        )
        if not requested_document_id:
            active_job = await find_active_job_for_document(
                db,
                user_id=current_user.user_id,
                document_id=effective_document_id,
            )
            if active_job is not None:
                raise_document_ingestion_conflict(
                    document_id=effective_document_id,
                    active_job_id=active_job.job_id,
                )
        job_metadata["document_id"] = effective_document_id
        job_metadata["namespace"] = effective_namespace

        # Enforce Layers 2-3 immediately before DB insert so the row lock
        # lifetime is limited to capacity check + create_job commit.
        await enforce_job_creation_capacity(
            request=http_request,
            db=db,
            current_user=current_user,
        )

        if payload.source_type == "file":
            # File-upload mode: reserve the job row first.
            assert payload.file_name is not None
            file_extension = os.path.splitext(payload.file_name)[1]
            s3_key = f"uploads/{job_id}{file_extension}"
            job_metadata["source_file_name"] = payload.file_name
            job_metadata["source_type"] = "file"

            # Create the waiting-file job row with the final S3 key in one insert.
            job_repo = JobRepository()
            try:
                job = await job_repo.create_job(
                    db=db,
                    job_id=job_id,
                    user_id=current_user.user_id,
                    job_type=job_type,
                    source_type="file",
                    file_path=None,  # The file has not been uploaded yet.
                    webhook_url=payload.webhook.url if payload.webhook else None,
                    metadata=job_metadata,
                    initial_state="waiting-file",
                    s3_key=s3_key,
                )
            except IntegrityError as exc:
                if is_active_document_job_unique_violation(exc):
                    raise_document_ingestion_conflict(document_id=effective_document_id)
                raise

            if not job:
                raise JobOperationException(
                    internal_message="Failed to create job in database"
                )

            # Generate the presigned upload URL.
            upload_service = FileUploadService()
            upload_info = await upload_service.generate_upload_url(
                job_id, file_extension
            )

            # 3. Cache job_metadata in Redis for two hours.
            from shared.services.redis.job_metadata_service import JobMetadataService

            metadata_service = JobMetadataService(redis_service)
            await metadata_service.save_metadata(job_id, job_metadata)

            # 4. Cache the basic job info in Redis for two hours.
            from datetime import datetime

            from shared.services.redis import JobInfoRedisService

            job_info_service = JobInfoRedisService(redis_service)
            job_info = {
                "job_id": job_id,
                "s3_key": s3_key,
                "user_id": current_user.user_id,
                "webhook_enabled": bool(payload.webhook and payload.webhook.url),
                "job_type": job_type,
                "source_type": "file",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            await job_info_service.save_job_info(job_id, job_info)

            logger.info(
                f"Job {job_id} upload_url returned to client: {upload_info['upload_url']}"
            )

            # Build the response payload.
            response = create_job_response(
                job_id=job_id,
                job=job,
                source_type="file",
                data_id=payload.data_id,
                namespace=effective_namespace,
                upload_url=upload_info["upload_url"],
                upload_headers=upload_info["upload_headers"],
                expires_in=upload_info["expires_in"],
            )

            return response

        else:
            # URL mode: create the job first, then download and upload asynchronously.
            try:
                assert payload.source_url is not None
                # Resolve file extension (URL path first, then Content-Type header)
                file_extension = await resolve_file_extension_async(payload.source_url)
                if not file_extension:
                    supported_formats = get_supported_formats()
                    raise ValidationException(
                        user_message=f"Unsupported URL file type. Supported formats: {supported_formats}",
                        violations=[
                            {
                                "field": "source_url",
                                "description": "URL file type not supported",
                            }
                        ],
                    )

                parsed_url = urlparse(payload.source_url)
                url_basename = str(os.path.basename(parsed_url.path))
                # Ensure source_file_name carries the correct extension.
                # URLs like arxiv.org/pdf/1706.03762 have no real extension in the path.
                if (
                    url_basename
                    and os.path.splitext(url_basename)[1].lower() == file_extension
                ):
                    source_file_name = url_basename
                elif url_basename:
                    source_file_name = f"{url_basename}{file_extension}"
                else:
                    source_file_name = (
                        f"url_file_{uuid.uuid4().hex[:8]}{file_extension}"
                    )

                s3_key = f"uploads/{job_id}{file_extension}"

                job_metadata.update(
                    {
                        "source_file_name": source_file_name,
                        "source_url": payload.source_url,
                        "source_type": "url",
                    }
                )

                # Create the waiting-file job row; the file will arrive asynchronously.
                job_repo = JobRepository()
                try:
                    job = await job_repo.create_job(
                        db=db,
                        job_id=job_id,
                        user_id=current_user.user_id,
                        job_type=job_type,
                        source_type="url",
                        file_path=None,
                        webhook_url=payload.webhook.url if payload.webhook else None,
                        metadata=job_metadata,
                        initial_state=JobStatus.WAITING_FILE.value,  # Reuse waiting-file for URL uploads.
                        s3_key=s3_key,  # Precomputed target S3 key.
                    )
                except IntegrityError as exc:
                    if is_active_document_job_unique_violation(exc):
                        raise_document_ingestion_conflict(
                            document_id=effective_document_id
                        )
                    raise

                if not job:
                    raise JobOperationException(
                        internal_message="Failed to create URL job in database"
                    )

                # Cache job_metadata in Redis for two hours.
                from shared.services.redis.job_metadata_service import (
                    JobMetadataService,
                )

                metadata_service = JobMetadataService(redis_service)
                await metadata_service.save_metadata(job_id, job_metadata)

                # Cache the basic job info in Redis for two hours.
                from datetime import datetime

                from shared.services.redis import JobInfoRedisService

                job_info_service = JobInfoRedisService(redis_service)
                job_info = {
                    "job_id": job_id,
                    "s3_key": s3_key,
                    "user_id": current_user.user_id,
                    "webhook_enabled": bool(payload.webhook and payload.webhook.url),
                    "job_type": job_type,
                    "source_type": "url",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                await job_info_service.save_job_info(job_id, job_info)

                # Start the URL download/upload task asynchronously in the worker.
                from shared.core.celery_app import get_celery_app

                celery_app = get_celery_app()
                upload_url_file_task = celery_app.signature(
                    "app.core.tasks.kb_tasks.upload_url_file_task"
                )
                upload_url_file_task.apply_async(
                    args=[job_id, payload.source_url, current_user.user_id],
                    kwargs={
                        "job_type": job_type,
                    },
                )

                # Build the response payload.
                response = create_job_response(
                    job_id=job_id,
                    job=job,
                    source_type="url",
                    data_id=payload.data_id,
                    namespace=effective_namespace,
                )

                return response

            except ValidationException:
                raise
            except WebhookConfigException:
                raise
            except ConflictException:
                raise
            except (RateLimitException, UnavailableException):
                raise
            except JobOperationException:
                raise
            except Exception as e:
                logger.error(f"Failed to create URL job: {e}")
                raise JobOperationException(
                    internal_message=f"URL job creation failed: {str(e)}"
                )

    except NotFoundException:
        raise
    except ValidationException:
        raise
    except ConflictException:
        raise
    except WebhookConfigException:
        raise
    except (RateLimitException, UnavailableException):
        raise
    except JobOperationException:
        raise
    except Exception as e:
        logger.error(f"Failed to create job: {e}")
        raise JobOperationException(internal_message=f"Job creation failed: {str(e)}")


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
        # start_time / end_time take priority over recent_days.
        if normalized_start_time:
            created_after = normalized_start_time
        created_before = normalized_end_time

        # Count matching rows.
        total_count = await job_repo.count_jobs_by_user(
            db=db,
            user_id=current_user.user_id,
            created_after=created_after,
            created_before=created_before,
            job_type=job_type,
            job_status=job_status,
        )

        # Fetch the matching jobs.
        jobs = await job_repo.get_jobs_by_user(
            db=db,
            user_id=current_user.user_id,
            limit=page_size,
            offset=(page - 1) * page_size,
            created_after=created_after,
            created_before=created_before,
            job_type=job_type,
            job_status=job_status,
        )

        # Build the response payload.
        job_responses = []
        upload_service = FileUploadService()
        from shared.models.schemas.job_metadata import JobMetadataHelper
        from shared.services.redis import RedisServiceFactory

        redis_service = RedisServiceFactory.get_service()
        for job in jobs:
            # Load job_metadata through the shared access path.
            job_metadata = await job_repo.get_job_metadata(
                db, job.job_id, redis_service
            )
            job_result = job.job_result
            status_for_api = to_job_status_value(job.status)

            result_url = None
            result = None
            result_url_expires_at = job.created_at  # Default to created_at.

            if job_result and job_result.result_s3_key:
                result_url_info = cast(
                    Dict[str, Any],
                    await upload_service.generate_download_url(
                        job_result.result_s3_key
                    ),
                )
                result_url = result_url_info["download_url"]

                # Read checksum-only data from inline_payload when present.
                if job_result.inline_payload:
                    result = job_result.inline_payload

                # Compute result_url_expires_at when a download URL was issued.
                if result_url:
                    expires_in = int(result_url_info.get("expires_in", 3600))
                    result_url_expires_at = utc_now_naive() + timedelta(
                        seconds=expires_in
                    )

            original_request = (
                job_metadata.get("original_request")
                if isinstance(job_metadata, dict)
                else {}
            )
            source_url = (
                original_request.get("source_url")
                if isinstance(original_request, dict)
                else None
            )
            file_name = None
            if source_url:
                parsed_source = urlparse(source_url)
                file_name = os.path.basename(parsed_source.path) or None
            if not file_name and isinstance(original_request, dict):
                file_name = original_request.get("file_name")
            file_extension = None
            if file_name:
                ext = os.path.splitext(file_name)[1]
                file_extension = ext[1:].upper() if ext else None

            parsing_params = {}
            if isinstance(original_request, dict):
                parsing_params = original_request.get("parsing_params") or {}
            if not parsing_params and isinstance(job_metadata, dict):
                parsing_params = job_metadata.get("parsing_params") or {}
            model = (
                parsing_params.get("model")
                if isinstance(parsing_params, dict)
                else None
            )
            ocr_enabled = (
                parsing_params.get("ocr_enabled")
                if isinstance(parsing_params, dict)
                else None
            )

            duration_seconds = None
            if job.updated_at and job.created_at:
                duration_seconds = (job.updated_at - job.created_at).total_seconds()

            job_responses.append(
                JobResultResponse(
                    job_id=job.job_id,
                    namespace=JobMetadataHelper.get_field(job_metadata, "namespace"),
                    document_id=resolve_public_document_id(job),
                    status=status_for_api,
                    source_type=job.source_type,
                    data_id=JobMetadataHelper.get_field(job_metadata, "data_id"),
                    created_at=require_utc(job.created_at, field_name="created_at"),
                    progress=None,  # The list view does not expose detailed progress.
                    error=_build_error_response(job, job_metadata),
                    result=result,
                    result_url=result_url,
                    result_url_expires_at=require_utc(
                        result_url_expires_at,
                        field_name="result_url_expires_at",
                    ),
                    file_name=file_name,
                    file_extension=file_extension,
                    model=model,
                    ocr_enabled=ocr_enabled,
                    duration_seconds=duration_seconds,
                    credits_spent=(
                        MicroDollar(job.credits_charged).to_credit()
                        if hasattr(job, "credits_charged")
                        else 0
                    ),
                )
            )

        # Compute the total page count.
        import math

        total_pages = math.ceil(total_count / page_size) if total_count > 0 else 0

        response = JobList(
            jobs=job_responses,
            total=total_count,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

        return response

    except Exception as e:
        logger.error(f"Failed to list jobs: {e}")
        raise JobOperationException(
            internal_message=f"Failed to get job list: {str(e)}"
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
    try:
        job_repo = JobRepository()

        # Load the job and verify access.
        job = await job_repo.get_job_by_id(db, job_id)
        check_job_permission(job, current_user.user_id, job_id)
        assert job is not None

        status_for_api = to_job_status_value(job.status)

        # Load detailed progress from Redis while the job is running.
        progress = None
        if status_for_api == "running":
            # TODO: Load detailed progress from Redis and convert it to the progress schema.
            # from shared.services.redis import RedisServiceFactory
            # redis_service = RedisServiceFactory.get_service()
            # from shared.utils.redis_key_builder import redis_key_builder

            # progress_key = redis_key_builder.task_progress(job_id)
            # progress = await redis_service.hgetall(progress_key)
            progress = {"total_pages": 10, "processed_pages": 5}

        # Load job_metadata through the shared access path.
        from shared.models.schemas.job_metadata import JobMetadataHelper
        from shared.services.redis import RedisServiceFactory

        redis_service = RedisServiceFactory.get_service()
        job_metadata = await job_repo.get_job_metadata(db, job_id, redis_service)

        # Result delivery fields.
        job_result = job.job_result
        result_url = None
        result = None
        result_url_expires_at = job.created_at  # Default to created_at.

        if job_result and job_result.result_s3_key:
            upload_service = FileUploadService()
            result_url_info = cast(
                Dict[str, Any],
                await upload_service.generate_download_url(job_result.result_s3_key),
            )
            result_url = result_url_info["download_url"]
            expires_in = int(result_url_info["expires_in"])

            # Read checksum/statistics data from inline_payload when present.
            if job_result.inline_payload:
                result = job_result.inline_payload

            # Compute result_url_expires_at when a download URL was issued.
            if result_url:
                from datetime import datetime, timedelta

                result_url_expires_at = datetime.now() + timedelta(seconds=expires_in)

        original_request = (
            job_metadata.get("original_request")
            if isinstance(job_metadata, dict)
            else {}
        )
        source_url = (
            original_request.get("source_url")
            if isinstance(original_request, dict)
            else None
        )
        file_name = None
        if source_url:
            parsed_source = urlparse(source_url)
            file_name = os.path.basename(parsed_source.path) or None
        if not file_name and isinstance(original_request, dict):
            file_name = original_request.get("file_name")
        file_extension = None
        if file_name:
            ext = os.path.splitext(file_name)[1]
            file_extension = ext[1:].upper() if ext else None

        parsing_params = {}
        if isinstance(original_request, dict):
            parsing_params = original_request.get("parsing_params") or {}
        if not parsing_params and isinstance(job_metadata, dict):
            parsing_params = job_metadata.get("parsing_params") or {}
        model = (
            parsing_params.get("model") if isinstance(parsing_params, dict) else None
        )
        ocr_enabled = (
            parsing_params.get("ocr_enabled")
            if isinstance(parsing_params, dict)
            else None
        )

        response_data = JobResultResponse(
            job_id=job.job_id,
            namespace=JobMetadataHelper.get_field(job_metadata, "namespace"),
            document_id=resolve_public_document_id(job),
            status=status_for_api,
            source_type=job.source_type,
            data_id=JobMetadataHelper.get_field(job_metadata, "data_id"),
            created_at=require_utc(job.created_at, field_name="created_at"),
            progress=progress,
            error=_build_error_response(job, job_metadata),
            result=result,
            result_url=result_url,
            result_url_expires_at=require_utc(
                result_url_expires_at,
                field_name="result_url_expires_at",
            ),
            file_name=file_name,
            file_extension=file_extension,
            model=model,
            ocr_enabled=ocr_enabled,
            duration_seconds=(
                (job.updated_at - job.created_at).total_seconds()
                if job.updated_at and job.created_at
                else None
            ),
            credits_spent=(
                MicroDollar(job.credits_charged).to_credit()
                if hasattr(job, "credits_charged")
                else 0
            ),
        )

        return response_data

    except NotFoundException:
        raise
    except PermissionDeniedException:
        raise
    except Exception as e:
        logger.error(f"Failed to get job result: {e}")
        raise JobOperationException(
            internal_message=f"Failed to get job result: {str(e)}"
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
    try:
        job_repo = JobRepository()

        # Load the job and verify access.
        job = await job_repo.get_job_by_id(db, job_id)
        check_job_permission(job, current_user.user_id, job_id)
        assert job is not None

        # Check the current job state.
        logger.info(f"Confirm upload - Job {job_id} current status: {job.status}")
        if job.status not in [JobStatus.PENDING.value, JobStatus.WAITING_FILE.value]:
            # If the webhook already advanced the job, return success idempotently.
            logger.info(f"Job {job_id} already processed, status: {job.status}")
            return {"message": "Job status already updated"}

        # Verify that the S3 object exists.
        if not job.s3_key:
            raise ValidationException(
                user_message="Job is missing S3 key information",
                violations=[
                    {"field": "s3_key", "description": "S3 key not set for this job"}
                ],
            )

        upload_service = FileUploadService()
        file_info = await upload_service.verify_s3_file_exists(job.s3_key)

        if not file_info.get("exists"):
            raise ValidationException(
                user_message="S3 file does not exist, please upload the file first",
                violations=[{"field": "file", "description": "File not found in S3"}],
            )

        # Advance the job state.
        await transition_to_uploaded(
            db, job_id, job.job_type, "manual_upload_completed"
        )

        # Start job processing.
        await start_workflow_for_job(
            db=db,
            job_id=job_id,
            job_type=job.job_type,
            source_type="file",
            user_id=current_user.user_id,
        )

        return {"message": "File upload confirmed; processing started"}

    except NotFoundException:
        raise
    except PermissionDeniedException:
        raise
    except ValidationException:
        raise
    except Exception as e:
        logger.error(f"Failed to confirm upload: {e}")
        raise JobOperationException(
            internal_message=f"Failed to confirm upload: {str(e)}"
        )
