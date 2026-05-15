from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional, cast
from urllib.parse import urlparse

from shared.core.billing import MicroDollar
from shared.core.exceptions.domain_exceptions import JobOperationException
from shared.models.schemas.job import JobResultResponse, StandardErrorObject
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.services.storage.file_upload_service import FileUploadService
from shared.utils.error_details import normalize_error_details
from shared.utils.utc_now import utc_now_naive

JobStatusValue = Literal[
    "pending", "waiting-file", "running", "converting", "done", "failed"
]


def build_error_response(
    job: Any, job_metadata: Optional[dict] = None
) -> Optional[StandardErrorObject]:
    if not job.error_message:
        return None

    error_details = None
    if job_metadata and isinstance(job_metadata, dict):
        error_details = normalize_error_details(job_metadata.get("error_details"))

    return StandardErrorObject(
        code=job.error_code or "UNKNOWN",
        message=job.error_message,
        request_id=job.job_id,
        details=error_details,
    )


def resolve_public_document_id(job: Any) -> Optional[str]:
    job_result = getattr(job, "job_result", None)
    published_document_id = getattr(job_result, "document_id", None)
    if isinstance(published_document_id, str) and published_document_id:
        return published_document_id

    return None


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def require_utc(dt: Optional[datetime], *, field_name: str) -> datetime:
    normalized_dt = ensure_utc(dt)
    if normalized_dt is None:
        raise JobOperationException(
            internal_message=f"Job is missing required datetime field: {field_name}"
        )
    return normalized_dt


def to_job_status_value(status: str) -> JobStatusValue:
    return cast(JobStatusValue, status)


def _resolve_original_request(job_metadata: Optional[dict[str, Any]]) -> dict[str, Any]:
    original_request = (
        job_metadata.get("original_request")
        if isinstance(job_metadata, dict)
        else {}
    )
    return original_request if isinstance(original_request, dict) else {}


def _resolve_source_file_name(original_request: dict[str, Any]) -> str | None:
    source_url = original_request.get("source_url")
    file_name = None
    if source_url:
        parsed_source = urlparse(str(source_url))
        file_name = os.path.basename(parsed_source.path) or None
    if not file_name:
        file_name = original_request.get("file_name")
    return str(file_name) if file_name else None


def _resolve_file_extension(file_name: str | None) -> str | None:
    if not file_name:
        return None
    extension = os.path.splitext(file_name)[1]
    return extension[1:].upper() if extension else None


def _resolve_parsing_params(
    job_metadata: Optional[dict[str, Any]],
    original_request: dict[str, Any],
) -> dict[str, Any]:
    parsing_params = original_request.get("parsing_params") or {}
    if not parsing_params and isinstance(job_metadata, dict):
        parsing_params = job_metadata.get("parsing_params") or {}
    return parsing_params if isinstance(parsing_params, dict) else {}


def _resolve_duration_seconds(job: Any) -> float | None:
    if job.updated_at and job.created_at:
        return (job.updated_at - job.created_at).total_seconds()
    return None


async def _resolve_result_delivery(job: Any) -> tuple[dict[str, Any] | None, str | None, datetime]:
    job_result = job.job_result
    result_url = None
    result = None
    result_url_expires_at = job.created_at

    if job_result and job_result.result_s3_key:
        upload_service = FileUploadService()
        result_url_info = await upload_service.generate_download_url(
            job_result.result_s3_key
        )
        result_url = result_url_info["download_url"]

        if job_result.inline_payload:
            result = job_result.inline_payload

        if result_url:
            expires_in = int(result_url_info.get("expires_in", 3600))
            result_url_expires_at = utc_now_naive() + timedelta(seconds=expires_in)

    return result, result_url, result_url_expires_at


async def build_job_result_response(
    *,
    job: Any,
    job_metadata: Optional[dict[str, Any]],
    progress: dict[str, Any] | None,
) -> JobResultResponse:
    original_request = _resolve_original_request(job_metadata)
    file_name = _resolve_source_file_name(original_request)
    parsing_params = _resolve_parsing_params(job_metadata, original_request)
    result, result_url, result_url_expires_at = await _resolve_result_delivery(job)

    return JobResultResponse(
        job_id=job.job_id,
        namespace=JobMetadataHelper.get_field(job_metadata, "namespace"),
        document_id=resolve_public_document_id(job),
        status=to_job_status_value(job.status),
        source_type=job.source_type,
        data_id=JobMetadataHelper.get_field(job_metadata, "data_id"),
        created_at=require_utc(job.created_at, field_name="created_at"),
        progress=progress,
        error=build_error_response(job, job_metadata),
        result=result,
        result_url=result_url,
        result_url_expires_at=require_utc(
            result_url_expires_at,
            field_name="result_url_expires_at",
        ),
        file_name=file_name,
        file_extension=_resolve_file_extension(file_name),
        model=parsing_params.get("model"),
        ocr_enabled=parsing_params.get("ocr_enabled"),
        duration_seconds=_resolve_duration_seconds(job),
        credits_spent=(
            MicroDollar(job.credits_charged).to_credit()
            if hasattr(job, "credits_charged")
            else 0
        ),
    )
