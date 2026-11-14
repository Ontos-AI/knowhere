from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Literal, Optional, cast
from urllib.parse import urlparse

from shared.core.billing import MicroDollar
from shared.core.exceptions.domain_exceptions import JobOperationException
from shared.models.schemas.job import JobResultResponse, StandardErrorObject
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.services.jobs.result_delivery import JobResultDeliveryResolver
from shared.services.jobs.progressive_ingestion import read_progressive_state, publish_snapshot_descriptors
from shared.services.storage.job_file_storage import JobFileStorage
from shared.utils.error_details import normalize_error_details

JobStatusValue = Literal[
    "pending", "waiting-file", "running", "converting", "done", "failed"
]
_CHARGED_BILLING_STATUS: str = "charged"


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


def resolve_job_document_id(
    job: Any, job_metadata: Optional[dict[str, Any]]
) -> Optional[str]:
    published_document_id = resolve_public_document_id(job)
    if published_document_id:
        return published_document_id
    return JobMetadataHelper.get_document_id(job_metadata)


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
    return JobMetadataHelper.get_original_request(job_metadata)


def _resolve_source_file_name(original_request: dict[str, Any]) -> str | None:
    requested_name = original_request.get("file_name")
    source_url = original_request.get("source_url")
    url_basename = None
    if source_url:
        url_basename = os.path.basename(urlparse(str(source_url)).path) or None
    if isinstance(requested_name, str) and requested_name.strip():
        file_name = requested_name.strip()
        if not os.path.splitext(file_name)[1]:
            url_extension = os.path.splitext(url_basename or "")[1]
            if url_extension:
                file_name = f"{file_name}{url_extension}"
        return file_name
    return str(url_basename) if url_basename else None


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
    if not parsing_params:
        parsing_params = JobMetadataHelper.get_parsing_params_dict(job_metadata)
    return parsing_params if isinstance(parsing_params, dict) else {}


def _resolve_duration_seconds(job: Any) -> float | None:
    if job.updated_at and job.created_at:
        return (job.updated_at - job.created_at).total_seconds()
    return None


def _resolve_credits_spent(job: Any) -> float:
    if to_job_status_value(job.status) == "failed" and getattr(job, "failure_stage", None) != "indexing":
        return 0.0

    billing_status = getattr(job, "billing_status", None)
    if billing_status != _CHARGED_BILLING_STATUS:
        return 0.0

    return MicroDollar(getattr(job, "credits_charged", 0) or 0).to_credit()


async def _resolve_result_delivery(
    job: Any,
    *, shouldSignResultURL: bool = True,
) -> tuple[dict[str, Any] | None, str | None, datetime]:
    default_expires_at = require_utc(
        job.created_at,
        field_name="created_at",
    )
    if not shouldSignResultURL:
        inlinePayload: object = getattr(getattr(job, "job_result", None), "inline_payload", None)
        result: dict[str, object] | None = cast(dict[str, object], inlinePayload) if isinstance(inlinePayload, dict) else None
        return result, None, default_expires_at
    delivery = await JobResultDeliveryResolver().resolveAsync(
        job.job_result,
        default_expires_at=default_expires_at,
    )
    return (
        delivery.result,
        delivery.result_url,
        delivery.result_url_expires_at or default_expires_at,
    )


async def build_job_result_response(
    *,
    job: Any,
    job_metadata: Optional[dict[str, Any]],
    progress: dict[str, Any] | None,
    shouldSignResultURL: bool = True,
) -> JobResultResponse:
    original_request = _resolve_original_request(job_metadata)
    file_name = _resolve_source_file_name(original_request)
    parsing_params = _resolve_parsing_params(job_metadata, original_request)
    result, result_url, result_url_expires_at = await _resolve_result_delivery(job, shouldSignResultURL=shouldSignResultURL)
    progressive_state = read_progressive_state(
        getattr(getattr(job, "job_result", None), "document_metadata", None)
    )
    raw_page_count: object = getattr(job, "page_count", None) or (job_metadata or {}).get("page_count")
    page_count: int | None = raw_page_count if isinstance(raw_page_count, int) and not isinstance(raw_page_count, bool) and raw_page_count > 0 else None
    # A poll can cache metadata before the worker measures the source.
    raw_source_size: object = JobMetadataHelper.get_field(
        job.job_metadata, "source_size_bytes"
    )
    source_size_bytes: int | None = (
        raw_source_size
        if isinstance(raw_source_size, int)
        and not isinstance(raw_source_size, bool)
        and raw_source_size >= 0
        else None
    )

    return JobResultResponse(
        job_result_id=getattr(getattr(job, "job_result", None), "id", None),
        job_id=job.job_id,
        page_count=page_count,
        source_size_bytes=source_size_bytes,
        enhancement=progressive_state.enhancement if progressive_state else None,
        snapshots=(
            publish_snapshot_descriptors(progressive_state, JobFileStorage())
            if progressive_state and shouldSignResultURL
            else None
        ),
        current_snapshot_id=progressive_state.current_snapshot_id if progressive_state else None,
        namespace=JobMetadataHelper.get_namespace(job_metadata),
        document_id=resolve_job_document_id(job, job_metadata),
        status=to_job_status_value(job.status),
        parsing_status=("running" if getattr(job, "parsing_status", "pending") == "pending" and job.status in ("running", "converting")
                        else getattr(job, "parsing_status", "completed" if job.status == "done" else "failed" if job.status == "failed" else "pending")),
        indexing_status=getattr(job, "indexing_status", "completed" if job.status == "done" else "not_started"),
        failure_stage=getattr(job, "failure_stage", None),
        source_type=job.source_type,
        data_id=JobMetadataHelper.get_data_id(job_metadata),
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
        credits_spent=_resolve_credits_spent(job),
    )
