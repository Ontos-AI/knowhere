from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast
from urllib.parse import urlparse

from app.repositories.job_repository import JobRepository
from app.services.document_ingestion.scope_service import (
    is_active_document_job_unique_violation,
    raise_document_ingestion_conflict,
)
from app.services.job_response_projection import to_job_status_value
from app.services.rate_limit.data_structures import CurrentUser
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import (
    JobOperationException,
    ValidationException,
)
from shared.core.state_machine.states import JobStatus
from shared.models.database.job import Job
from shared.models.schemas.job import JobCreate, JobResponse
from shared.services.redis import JobInfoRedisService, RedisServiceFactory
from shared.services.redis.job_metadata_service import JobMetadataService
from shared.services.storage.file_upload_service import FileUploadService
from shared.utils.url_file_type import resolve_file_extension_async

_JOB_TYPE_KB_MANAGEMENT = "kb_management"
JobMetadata = dict[str, object]
UploadHeaders = dict[str, str]


@dataclass(frozen=True)
class ResolvedDocumentIngestionScope:
    job_metadata: JobMetadata
    document_id: str
    namespace: str


class DocumentIngestionCreationService:
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
        job_id: str,
        current_user: CurrentUser,
        scope: ResolvedDocumentIngestionScope,
    ) -> JobResponse:
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
                job_type=_JOB_TYPE_KB_MANAGEMENT,
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
            "job_type": _JOB_TYPE_KB_MANAGEMENT,
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
            raise ValidationException(
                user_message=(
                    "Unsupported URL file type. Supported formats: "
                    f"{_get_supported_formats()}"
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


def _get_supported_formats() -> str:
    from shared.core.config import settings

    return ", ".join(sorted(settings.get_supported_extensions()))


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
        kwargs={"job_type": _JOB_TYPE_KB_MANAGEMENT},
    )
