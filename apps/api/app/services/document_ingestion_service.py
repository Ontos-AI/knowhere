from __future__ import annotations

import os
import uuid
from typing import cast

from app.services.document_ingestion_confirmation_service import (
    DocumentIngestionConfirmationService,
)
from app.services.document_ingestion_creation_service import (
    DocumentIngestionCreationService,
    ResolvedDocumentIngestionScope,
)
from app.services.job_document_scope_service import (
    find_active_job_for_document,
    raise_document_ingestion_conflict,
    resolve_effective_document_scope,
)
from app.services.rate_limit.data_structures import CurrentUser
from app.services.rate_limit.dependencies import enforce_job_creation_capacity
from fastapi import Request
from loguru import logger
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
from shared.models.schemas.job import ConfirmUploadRequest, JobCreate, JobResponse
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.utils.url_file_type import resolve_file_extension_async
from shared.utils.url_security import validate_http_url_and_resolve_ip_async

JobMetadata = dict[str, object]


class DocumentIngestionService:
    def __init__(
        self,
        *,
        creation_service: DocumentIngestionCreationService | None = None,
        confirmation_service: DocumentIngestionConfirmationService | None = None,
    ) -> None:
        self._creation_service = creation_service or DocumentIngestionCreationService()
        self._confirmation_service = (
            confirmation_service or DocumentIngestionConfirmationService()
        )

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

            return await self._creation_service.create_job(
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
            return await self._confirmation_service.confirm_upload(
                db=db,
                job_id=job_id,
                user_id=user_id,
            )
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
            raise ValidationException(
                user_message=(
                    "Unsupported file type. Supported formats: "
                    f"{_get_supported_formats()}"
                ),
                violations=[
                    {"field": "file_name", "description": "File type not supported"}
                ],
            )

        if payload.source_type == "url":
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


def _get_supported_formats() -> str:
    return ", ".join(sorted(settings.get_supported_extensions()))


def _is_supported_file_name(file_name: str) -> bool:
    if not file_name:
        return False
    file_extension = os.path.splitext(file_name)[1].lower()
    return file_extension in settings.get_supported_extensions()
