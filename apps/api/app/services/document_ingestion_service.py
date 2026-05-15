from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast
from urllib.parse import urlparse

from app.repositories.job_repository import JobRepository
from app.services.job_document_scope_service import (
    find_active_job_for_document,
    is_active_document_job_unique_violation,
    raise_document_ingestion_conflict,
    resolve_effective_document_scope,
)
from app.services.job_read_service import check_job_permission
from app.services.job_response_projection import to_job_status_value
from app.services.knowledge.kb_orchestrator import KBOrchestrator
from app.services.rate_limit.data_structures import CurrentUser
from app.services.rate_limit.dependencies import enforce_job_creation_capacity
from app.services.state_machine import JobStateMachine
from fastapi import Request
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.config import settings
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
from shared.models.database.job import Job
from shared.models.schemas.job import ConfirmUploadRequest, JobCreate, JobResponse
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.services.redis import JobInfoRedisService, RedisServiceFactory
from shared.services.redis.job_metadata_service import JobMetadataService
from shared.services.storage.file_upload_service import FileUploadService
from shared.utils.url_file_type import resolve_file_extension_async
from shared.utils.url_security import validate_http_url_and_resolve_ip_async

JOB_TYPE_KB_MANAGEMENT = "kb_management"
JobMetadata = dict[str, object]
UploadHeaders = dict[str, str]


@dataclass(frozen=True)
class ResolvedDocumentIngestionScope:
    job_metadata: JobMetadata
    document_id: str
    namespace: str


def _get_supported_formats() -> str:
    return ", ".join(sorted(settings.get_supported_extensions()))


def _is_supported_file_name(file_name: str) -> bool:
    if not file_name:
        return False
    file_extension = os.path.splitext(file_name)[1].lower()
    return file_extension in settings.get_supported_extensions()


def _build_job_response(
    *,
    job_id: str,
    job: Job,
    source_type: str,
    data_id: str | None,
    namespace: str | None = None,
    document_id: str | None = None,
    upload_url: str | None = None,
    upload_headers: UploadHeaders | None = None,
    expires_in: int | None = None,
) -> JobResponse:
    return JobResponse(
        job_id=job_id,
        status=to_job_status_value(job.status),
        source_type=source_type,
        data_id=data_id,
        namespace=namespace,
        document_id=document_id,
        created_at=job.created_at,
        upload_url=upload_url,
        upload_headers=upload_headers,
        expires_in=expires_in,
    )


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
        kwargs={"job_type": JOB_TYPE_KB_MANAGEMENT},
    )


async def _transition_job_to_uploaded(
    db: AsyncSession,
    *,
    job_id: str,
    trigger: str = "manual_upload_completed",
) -> None:
    state_machine = JobStateMachine()
    await state_machine.transition(
        db,
        job_id,
        JobStatus.PENDING.value,
        trigger,
        None,
        "system",
    )


async def _start_job_workflow(
    db: AsyncSession,
    *,
    job_id: str,
    job_type: str,
    source_type: str,
    user_id: str,
    file_path: str | None = None,
    file_url: str | None = None,
) -> None:
    if job_type == JOB_TYPE_KB_MANAGEMENT:
        orchestrator = KBOrchestrator()
        await orchestrator.start_workflow(
            db=db,
            job_id=job_id,
            source_type=source_type,
            file_path=file_path,
            file_url=file_url,
            user_id=user_id,
        )
        return

    raise ValidationException(
        user_message="Unsupported job type",
        violations=[
            {
                "field": "job_type",
                "description": f"Job type '{job_type}' is not supported",
            }
        ],
    )


class DocumentIngestionService:
    def __init__(
        self,
        *,
        job_repository: JobRepository | None = None,
        file_upload_service: FileUploadService | None = None,
    ) -> None:
        self._job_repository = job_repository or JobRepository()
        self._file_upload_service = file_upload_service or FileUploadService()

    async def create_job(
        self,
        db: AsyncSession,
        *,
        payload: JobCreate,
        current_user: CurrentUser,
        request: Request,
    ) -> JobResponse:
        try:
            job_id = f"job_{uuid.uuid4().hex[:12]}"
            await self._validate_create_payload(payload)
            scope = await self._resolve_scope(
                db,
                payload=payload,
                current_user=current_user,
            )

            await enforce_job_creation_capacity(
                request=request,
                db=db,
                current_user=current_user,
            )

            if payload.source_type == "file":
                return await self._create_file_job(
                    db,
                    payload=payload,
                    job_id=job_id,
                    current_user=current_user,
                    scope=scope,
                )
            return await self._create_url_job(
                db,
                payload=payload,
                job_id=job_id,
                current_user=current_user,
                scope=scope,
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

    async def confirm_upload(
        self,
        db: AsyncSession,
        *,
        job_id: str,
        request_payload: ConfirmUploadRequest | None,
        user_id: str,
    ) -> dict[str, str]:
        del request_payload

        try:
            job = await self._job_repository.get_job_by_id(db, job_id)
            check_job_permission(job, user_id, job_id)
            assert job is not None

            logger.info(f"Confirm upload - Job {job_id} current status: {job.status}")
            if job.status not in [JobStatus.PENDING.value, JobStatus.WAITING_FILE.value]:
                logger.info(f"Job {job_id} already processed, status: {job.status}")
                return {"message": "Job status already updated"}

            if not job.s3_key:
                raise ValidationException(
                    user_message="Job is missing S3 key information",
                    violations=[
                        {
                            "field": "s3_key",
                            "description": "S3 key not set for this job",
                        }
                    ],
                )

            file_info = await self._file_upload_service.verify_s3_file_exists(job.s3_key)
            if not bool(file_info.get("exists")):
                raise ValidationException(
                    user_message="S3 file does not exist, please upload the file first",
                    violations=[
                        {"field": "file", "description": "File not found in S3"}
                    ],
                )

            await _transition_job_to_uploaded(db, job_id=job_id)
            await _start_job_workflow(
                db=db,
                job_id=job_id,
                job_type=job.job_type,
                source_type="file",
                user_id=user_id,
            )
            return {"message": "File upload confirmed; processing started"}
        except NotFoundException:
            raise
        except PermissionDeniedException:
            raise
        except ValidationException:
            raise
        except Exception as exc:
            logger.error(f"Failed to confirm upload: {exc}")
            raise JobOperationException(
                internal_message=f"Failed to confirm upload: {str(exc)}"
            )

    async def _validate_create_payload(self, payload: JobCreate) -> None:
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
                    internal_message=(
                        "Webhook validation failed: "
                        f"{validation_result.error_message}"
                    ),
                )

        if (
            payload.source_type == "file"
            and payload.file_name
            and not _is_supported_file_name(payload.file_name)
        ):
            supported_formats = _get_supported_formats()
            raise ValidationException(
                user_message=(
                    "Unsupported file type. Supported formats: "
                    f"{supported_formats}"
                ),
                violations=[
                    {"field": "file_name", "description": "File type not supported"}
                ],
            )

        if payload.source_type == "url":
            assert payload.source_url is not None
            file_extension = await resolve_file_extension_async(payload.source_url)
            if not file_extension:
                supported_formats = _get_supported_formats()
                raise ValidationException(
                    user_message=(
                        "Unsupported URL file type. Supported formats: "
                        f"{supported_formats}"
                    ),
                    violations=[
                        {
                            "field": "source_url",
                            "description": "URL file type not supported",
                        }
                    ],
                )

    async def _resolve_scope(
        self,
        db: AsyncSession,
        *,
        payload: JobCreate,
        current_user: CurrentUser,
    ) -> ResolvedDocumentIngestionScope:
        job_metadata = cast(JobMetadata, JobMetadataHelper.create_from_request(payload))
        requested_document_id = cast(str | None, job_metadata.get("document_id"))
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
            requested_namespace=cast(str | None, payload.namespace),
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
        return ResolvedDocumentIngestionScope(
            job_metadata=job_metadata,
            document_id=effective_document_id,
            namespace=effective_namespace,
        )

    async def _create_waiting_job(
        self,
        db: AsyncSession,
        *,
        job_id: str,
        user_id: str,
        source_type: str,
        webhook_url: str | None,
        job_metadata: JobMetadata,
        s3_key: str,
        document_id: str,
    ) -> Job:
        try:
            job = await self._job_repository.create_job(
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
                raise_document_ingestion_conflict(document_id=document_id)
            raise

        if job is None:
            raise JobOperationException(
                internal_message="Failed to create job in database"
            )
        return job

    async def _cache_job_creation_state(
        self,
        *,
        job_id: str,
        s3_key: str,
        user_id: str,
        webhook_enabled: bool,
        source_type: str,
        job_metadata: JobMetadata,
    ) -> None:
        redis_service = RedisServiceFactory.get_service()
        metadata_service = JobMetadataService(redis_service)
        await metadata_service.save_metadata(job_id, job_metadata)

        job_info_service = JobInfoRedisService(redis_service)
        job_info: dict[str, object] = {
            "job_id": job_id,
            "s3_key": s3_key,
            "user_id": user_id,
            "webhook_enabled": webhook_enabled,
            "job_type": JOB_TYPE_KB_MANAGEMENT,
            "source_type": source_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await job_info_service.save_job_info(job_id, job_info)

    async def _create_file_job(
        self,
        db: AsyncSession,
        *,
        payload: JobCreate,
        job_id: str,
        current_user: CurrentUser,
        scope: ResolvedDocumentIngestionScope,
    ) -> JobResponse:
        assert payload.file_name is not None
        file_extension = os.path.splitext(payload.file_name)[1]
        s3_key = f"uploads/{job_id}{file_extension}"
        scope.job_metadata["source_file_name"] = payload.file_name
        scope.job_metadata["source_type"] = "file"

        job = await self._create_waiting_job(
            db,
            job_id=job_id,
            user_id=current_user.user_id,
            source_type="file",
            webhook_url=payload.webhook.url if payload.webhook else None,
            job_metadata=scope.job_metadata,
            s3_key=s3_key,
            document_id=scope.document_id,
        )

        upload_info = await self._file_upload_service.generate_upload_url(
            job_id,
            file_extension,
        )
        upload_url = cast(str, upload_info["upload_url"])
        upload_headers = cast(UploadHeaders, upload_info["upload_headers"])
        expires_in = cast(int, upload_info["expires_in"])

        await self._cache_job_creation_state(
            job_id=job_id,
            s3_key=s3_key,
            user_id=current_user.user_id,
            webhook_enabled=bool(payload.webhook and payload.webhook.url),
            source_type="file",
            job_metadata=scope.job_metadata,
        )

        logger.info(f"Job {job_id} upload_url returned to client: {upload_url}")
        return _build_job_response(
            job_id=job_id,
            job=job,
            source_type="file",
            data_id=payload.data_id,
            namespace=scope.namespace,
            upload_url=upload_url,
            upload_headers=upload_headers,
            expires_in=expires_in,
        )

    async def _create_url_job(
        self,
        db: AsyncSession,
        *,
        payload: JobCreate,
        job_id: str,
        current_user: CurrentUser,
        scope: ResolvedDocumentIngestionScope,
    ) -> JobResponse:
        assert payload.source_url is not None
        file_extension = await resolve_file_extension_async(payload.source_url)
        if not file_extension:
            supported_formats = _get_supported_formats()
            raise ValidationException(
                user_message=(
                    "Unsupported URL file type. Supported formats: "
                    f"{supported_formats}"
                ),
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
        scope.job_metadata.update(
            {
                "source_file_name": source_file_name,
                "source_url": payload.source_url,
                "source_type": "url",
            }
        )

        job = await self._create_waiting_job(
            db,
            job_id=job_id,
            user_id=current_user.user_id,
            source_type="url",
            webhook_url=payload.webhook.url if payload.webhook else None,
            job_metadata=scope.job_metadata,
            s3_key=s3_key,
            document_id=scope.document_id,
        )

        await self._cache_job_creation_state(
            job_id=job_id,
            s3_key=s3_key,
            user_id=current_user.user_id,
            webhook_enabled=bool(payload.webhook and payload.webhook.url),
            source_type="url",
            job_metadata=scope.job_metadata,
        )
        _schedule_url_upload(
            job_id=job_id,
            source_url=payload.source_url,
            user_id=current_user.user_id,
        )

        return _build_job_response(
            job_id=job_id,
            job=job,
            source_type="url",
            data_id=payload.data_id,
            namespace=scope.namespace,
        )
