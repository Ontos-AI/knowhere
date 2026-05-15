from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Optional, cast
from urllib.parse import urlparse

from app.repositories.job_repository import JobRepository
from app.services.job_document_scope_service import (
    find_active_job_for_document,
    is_active_document_job_unique_violation,
    raise_document_ingestion_conflict,
    resolve_effective_document_scope,
)
from app.services.rate_limit.data_structures import CurrentUser
from fastapi import Request
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    ConflictException,
    JobOperationException,
    NotFoundException,
    RateLimitException,
    UnavailableException,
    ValidationException,
)
from shared.core.exceptions.webhook_exceptions import WebhookConfigException
from shared.core.state_machine.states import JobStatus
from shared.models.schemas.job import JobCreate, JobResponse
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.services.redis import JobInfoRedisService, RedisServiceFactory
from shared.services.redis.job_metadata_service import JobMetadataService
from shared.services.storage.file_upload_service import FileUploadService
from shared.utils.url_file_type import resolve_file_extension_async
from shared.utils.url_security import validate_http_url_and_resolve_ip_async

JOB_TYPE_KB_MANAGEMENT = "kb_management"


def get_supported_formats() -> str:
    return ", ".join(sorted(settings.get_supported_extensions()))


def validate_file_type(file_name: str) -> bool:
    if not file_name:
        return False
    file_extension = os.path.splitext(file_name)[1].lower()
    return file_extension in settings.get_supported_extensions()


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


async def _validate_create_job_payload(payload: JobCreate) -> None:
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

    if payload.webhook and payload.webhook.url:
        validation_result = await validate_http_url_and_resolve_ip_async(
            payload.webhook.url,
        )
        if not validation_result.is_valid:
            raise WebhookConfigException(
                user_message="Invalid webhook URL",
                internal_message=f"Webhook validation failed: {validation_result.error_message}",
            )

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

    if payload.source_type == "url":
        assert payload.source_url is not None
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


async def _resolve_job_metadata(
    db: AsyncSession,
    *,
    payload: JobCreate,
    current_user: CurrentUser,
) -> tuple[dict, str, str]:
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
    return job_metadata, effective_document_id, effective_namespace


async def _cache_job_creation_state(
    *,
    job_id: str,
    s3_key: str,
    user_id: str,
    webhook_enabled: bool,
    source_type: str,
    job_metadata: dict,
) -> None:
    redis_service = RedisServiceFactory.get_service()
    metadata_service = JobMetadataService(redis_service)
    await metadata_service.save_metadata(job_id, job_metadata)

    job_info_service = JobInfoRedisService(redis_service)
    job_info = {
        "job_id": job_id,
        "s3_key": s3_key,
        "user_id": user_id,
        "webhook_enabled": webhook_enabled,
        "job_type": JOB_TYPE_KB_MANAGEMENT,
        "source_type": source_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await job_info_service.save_job_info(job_id, job_info)


async def _create_waiting_job(
    db: AsyncSession,
    *,
    job_id: str,
    user_id: str,
    source_type: str,
    webhook_url: str | None,
    job_metadata: dict,
    s3_key: str,
    effective_document_id: str,
):
    job_repo = JobRepository()
    try:
        return await job_repo.create_job(
            db=db,
            job_id=job_id,
            user_id=user_id,
            job_type=JOB_TYPE_KB_MANAGEMENT,
            source_type=source_type,
            file_path=None,
            webhook_url=webhook_url,
            metadata=job_metadata,
            initial_state=JobStatus.WAITING_FILE.value,
            s3_key=s3_key,
        )
    except IntegrityError as exc:
        if is_active_document_job_unique_violation(exc):
            raise_document_ingestion_conflict(document_id=effective_document_id)
        raise


def _resolve_url_source_file_name(*, source_url: str, file_extension: str) -> str:
    parsed_url = urlparse(source_url)
    url_basename = str(os.path.basename(parsed_url.path))
    if url_basename and os.path.splitext(url_basename)[1].lower() == file_extension:
        return url_basename
    if url_basename:
        return f"{url_basename}{file_extension}"
    return f"url_file_{uuid.uuid4().hex[:8]}{file_extension}"


def _schedule_url_upload(*, job_id: str, source_url: str, user_id: str) -> None:
    from shared.core.celery_app import get_celery_app

    celery_app = get_celery_app()
    upload_url_file_task = celery_app.signature(
        "app.core.tasks.kb_tasks.upload_url_file_task"
    )
    upload_url_file_task.apply_async(
        args=[job_id, source_url, user_id],
        kwargs={
            "job_type": JOB_TYPE_KB_MANAGEMENT,
        },
    )


async def _create_file_job(
    db: AsyncSession,
    *,
    payload: JobCreate,
    job_id: str,
    current_user: CurrentUser,
    job_metadata: dict,
    effective_document_id: str,
    effective_namespace: str,
) -> JobResponse:
    assert payload.file_name is not None
    file_extension = os.path.splitext(payload.file_name)[1]
    s3_key = f"uploads/{job_id}{file_extension}"
    job_metadata["source_file_name"] = payload.file_name
    job_metadata["source_type"] = "file"

    job = await _create_waiting_job(
        db,
        job_id=job_id,
        user_id=current_user.user_id,
        source_type="file",
        webhook_url=payload.webhook.url if payload.webhook else None,
        job_metadata=job_metadata,
        s3_key=s3_key,
        effective_document_id=effective_document_id,
    )
    if not job:
        raise JobOperationException(
            internal_message="Failed to create job in database"
        )

    upload_service = FileUploadService()
    upload_info = await upload_service.generate_upload_url(job_id, file_extension)

    await _cache_job_creation_state(
        job_id=job_id,
        s3_key=s3_key,
        user_id=current_user.user_id,
        webhook_enabled=bool(payload.webhook and payload.webhook.url),
        source_type="file",
        job_metadata=job_metadata,
    )

    logger.info(
        f"Job {job_id} upload_url returned to client: {upload_info['upload_url']}"
    )
    return create_job_response(
        job_id=job_id,
        job=job,
        source_type="file",
        data_id=payload.data_id,
        namespace=effective_namespace,
        upload_url=upload_info["upload_url"],
        upload_headers=upload_info["upload_headers"],
        expires_in=upload_info["expires_in"],
    )


async def _create_url_job(
    db: AsyncSession,
    *,
    payload: JobCreate,
    job_id: str,
    current_user: CurrentUser,
    job_metadata: dict,
    effective_document_id: str,
    effective_namespace: str,
) -> JobResponse:
    assert payload.source_url is not None
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

    source_file_name = _resolve_url_source_file_name(
        source_url=payload.source_url,
        file_extension=file_extension,
    )
    s3_key = f"uploads/{job_id}{file_extension}"
    job_metadata.update(
        {
            "source_file_name": source_file_name,
            "source_url": payload.source_url,
            "source_type": "url",
        }
    )

    job = await _create_waiting_job(
        db,
        job_id=job_id,
        user_id=current_user.user_id,
        source_type="url",
        webhook_url=payload.webhook.url if payload.webhook else None,
        job_metadata=job_metadata,
        s3_key=s3_key,
        effective_document_id=effective_document_id,
    )
    if not job:
        raise JobOperationException(
            internal_message="Failed to create URL job in database"
        )

    await _cache_job_creation_state(
        job_id=job_id,
        s3_key=s3_key,
        user_id=current_user.user_id,
        webhook_enabled=bool(payload.webhook and payload.webhook.url),
        source_type="url",
        job_metadata=job_metadata,
    )
    _schedule_url_upload(
        job_id=job_id,
        source_url=payload.source_url,
        user_id=current_user.user_id,
    )

    return create_job_response(
        job_id=job_id,
        job=job,
        source_type="url",
        data_id=payload.data_id,
        namespace=effective_namespace,
    )


async def create_job_from_request(
    db: AsyncSession,
    *,
    payload: JobCreate,
    current_user: CurrentUser,
    enforce_capacity,
    request: Request,
) -> JobResponse:
    try:
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        await _validate_create_job_payload(payload)
        (
            job_metadata,
            effective_document_id,
            effective_namespace,
        ) = await _resolve_job_metadata(
            db,
            payload=payload,
            current_user=current_user,
        )

        await enforce_capacity(
            request=request,
            db=db,
            current_user=current_user,
        )

        if payload.source_type == "file":
            return await _create_file_job(
                db,
                payload=payload,
                job_id=job_id,
                current_user=current_user,
                job_metadata=job_metadata,
                effective_document_id=effective_document_id,
                effective_namespace=effective_namespace,
            )
        return await _create_url_job(
            db,
            payload=payload,
            job_id=job_id,
            current_user=current_user,
            job_metadata=job_metadata,
            effective_document_id=effective_document_id,
            effective_namespace=effective_namespace,
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
    except Exception as exc:
        logger.error(f"Failed to create job: {exc}")
        raise JobOperationException(
            internal_message=f"Job creation failed: {str(exc)}"
        )
