from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from typing import cast

from app.services.document_ingestion.confirmation_service import (
    DocumentIngestionConfirmationService,
)
from app.services.document_ingestion.command import (
    ApiVersion,
    DocumentIngestionCommand,
    build_v1_ingestion_command,
    build_v2_ingestion_command,
)
from app.services.document_ingestion.creation_service import (
    DocumentIngestionCreationService,
    ResolvedDocumentIngestionScope,
)
from app.services.document_ingestion.scope_service import (
    find_active_job_for_document,
    raise_document_ingestion_conflict,
    resolve_effective_document_scope,
)
from app.services.rate_limit.data_structures import CurrentUser
from app.services.rate_limit.job_admission_service import JobAdmissionService
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
from shared.models.schemas.job import ConfirmUploadRequest, JobCreateBase, JobResponse
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.services.http.url_file_type import resolve_file_extension_async
from shared.services.http.url_security import validate_http_url_and_resolve_ip_async

JobMetadata = dict[str, object]
_PUBLIC_MODE_SELECTOR_FIELDS = {"mode", "processing"}
_PARSE_TRACK_FIELD = "parse_track"
_PAGE_MEMORY_FIELD_PREFIX = "page_memory"
_PUBLIC_COMPATIBILITY_EXTRA_FIELDS = frozenset({_PARSE_TRACK_FIELD, "result_mode"})
IngestionCommandFactory = Callable[[str], DocumentIngestionCommand]


class DocumentIngestionService:
    def __init__(
        self,
        *,
        creation_service: DocumentIngestionCreationService | None = None,
        confirmation_service: DocumentIngestionConfirmationService | None = None,
        job_admission_service: JobAdmissionService | None = None,
    ) -> None:
        self._creation_service = creation_service or DocumentIngestionCreationService()
        self._confirmation_service = (
            confirmation_service or DocumentIngestionConfirmationService()
        )
        self._job_admission_service = job_admission_service or JobAdmissionService()

    async def create_v1_job(
        self,
        db: AsyncSession,
        *,
        payload: JobCreateBase,
        current_user: CurrentUser,
    ) -> JobResponse:
        return await self._create_job_with_command_factory(
            db,
            payload=payload,
            current_user=current_user,
            api_version="v1",
            build_command=lambda _file_extension: build_v1_ingestion_command(
                payload=payload,
            ),
        )

    async def create_v2_job(
        self,
        db: AsyncSession,
        *,
        payload: JobCreateBase,
        current_user: CurrentUser,
    ) -> JobResponse:
        return await self._create_job_with_command_factory(
            db,
            payload=payload,
            current_user=current_user,
            api_version="v2",
            build_command=lambda file_extension: build_v2_ingestion_command(
                payload=payload,
                file_extension=file_extension,
            ),
        )

    async def _create_job_with_command_factory(
        self,
        db: AsyncSession,
        *,
        payload: JobCreateBase,
        current_user: CurrentUser,
        api_version: ApiVersion,
        build_command: IngestionCommandFactory,
    ) -> JobResponse:
        try:
            job_id = f"job_{uuid.uuid4().hex[:12]}"
            file_extension = await self._validate_create_payload(
                payload,
                api_version=api_version,
            )
            command = build_command(file_extension)
            scope = await self._resolve_scope(
                db,
                command=command,
                current_user=current_user,
            )

            await self._job_admission_service.enforce_job_creation_capacity(
                db=db,
                current_user=current_user,
            )

            return await self._creation_service.create_job(
                db,
                command=command,
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
        except UnavailableException:
            raise
        except ValidationException:
            raise
        except Exception as exc:
            logger.error(f"Failed to confirm upload: {exc}")
            raise JobOperationException(
                internal_message=f"Failed to confirm upload: {str(exc)}"
            )

    async def _validate_create_payload(
        self,
        payload: JobCreateBase,
        *,
        api_version: ApiVersion,
    ) -> str:
        _validate_public_mode_selector_fields(payload, api_version=api_version)

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
            return file_extension.lower()
        elif payload.file_name:
            return os.path.splitext(payload.file_name)[1].lower()
        return ""

    async def _resolve_scope(
        self,
        db: AsyncSession,
        *,
        command: DocumentIngestionCommand,
        current_user: CurrentUser,
    ) -> ResolvedDocumentIngestionScope:
        payload = command.payload
        job_metadata = cast(
            JobMetadata,
            JobMetadataHelper.create_from_request(
                payload,
                api_version=command.api_version,
                parse_track=command.parse_track,
                processing_generation=command.processing_generation,
                page_memory_config=command.page_memory_config,
            ),
        )
        requested_document_id = JobMetadataHelper.get_document_id(job_metadata)
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

        JobMetadataHelper.set_document_scope(
            job_metadata,
            document_id=effective_document_id,
            namespace=effective_namespace,
        )
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


def _validate_public_mode_selector_fields(
    payload: JobCreateBase,
    *,
    api_version: ApiVersion,
) -> None:
    extra_fields = payload.model_extra or {}
    if _PARSE_TRACK_FIELD in extra_fields:
        _validate_deprecated_parse_track(
            extra_fields[_PARSE_TRACK_FIELD],
            api_version=api_version,
        )

    forbidden_fields = [
        field_name
        for field_name in sorted(extra_fields)
        if (
            field_name not in _PUBLIC_COMPATIBILITY_EXTRA_FIELDS
            and _is_forbidden_public_mode_field(field_name)
        )
    ]
    if forbidden_fields:
        raise ValidationException(
            user_message="Parser mode fields are selected by the API version",
            violations=[
                {
                    "field": field_name,
                    "description": "Remove this field and choose /v1/jobs or /v2/jobs",
                }
                for field_name in forbidden_fields
            ],
        )

    unsupported_fields = [
        field_name
        for field_name in sorted(extra_fields)
        if field_name not in _PUBLIC_COMPATIBILITY_EXTRA_FIELDS
        and field_name not in forbidden_fields
    ]
    if unsupported_fields:
        raise ValidationException(
            user_message="Unsupported job-create fields",
            violations=[
                {
                    "field": field_name,
                    "description": "Remove this unsupported top-level field",
                }
                for field_name in unsupported_fields
            ],
        )


def _validate_deprecated_parse_track(
    parse_track: object,
    *,
    api_version: ApiVersion,
) -> None:
    if api_version == "v1" and parse_track == "chunk":
        return

    if api_version == "v1" and parse_track == "page_memory":
        raise ValidationException(
            user_message="Use /v2/jobs for page-memory ingestion",
            violations=[
                {
                    "field": _PARSE_TRACK_FIELD,
                    "description": (
                        "parse_track=page_memory is not accepted on /v1/jobs"
                    ),
                }
            ],
        )

    raise ValidationException(
        user_message="Parser mode is selected by the API version",
        violations=[
            {
                "field": _PARSE_TRACK_FIELD,
                "description": "Remove parse_track and choose /v1/jobs or /v2/jobs",
            }
        ],
    )


def _is_forbidden_public_mode_field(field_name: str) -> bool:
    normalized_name = field_name.strip().lower()
    if normalized_name in _PUBLIC_MODE_SELECTOR_FIELDS:
        return True
    return normalized_name.startswith(_PAGE_MEMORY_FIELD_PREFIX)
