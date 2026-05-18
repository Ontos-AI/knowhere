from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shared.core.state_machine.states import JobStatus
from shared.core.error_details import normalize_error_details
from shared.core.serialization import make_json_safe


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def serialize_transition_metadata(
    metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not metadata:
        return None

    try:
        return make_json_safe(metadata)
    except Exception:
        return {"error": "metadata_serialization_failed"}


def build_failure_transition_metadata(
    *,
    error_message: str,
    error_code: str,
    error_details: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    normalized_details = normalize_error_details(error_details)
    transition_metadata = (metadata or {}).copy()
    transition_metadata["error_message"] = error_message
    transition_metadata["error_code"] = error_code
    if normalized_details:
        transition_metadata["error_details"] = normalized_details
    return normalized_details, transition_metadata


def build_retry_transition(
    *,
    current_state: str,
    retry_metadata: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    retry_target = (
        JobStatus.PENDING.value
        if current_state == JobStatus.FAILED.value
        else current_state
    )

    resolved_metadata = retry_metadata or {}
    resolved_metadata["retry_reason"] = "task_retry"
    resolved_metadata["retry_timestamp"] = str(int(datetime.now(timezone.utc).timestamp()))
    resolved_metadata["retry_count"] = resolved_metadata.get("retry_count", 0) + 1
    return retry_target, resolved_metadata


def build_progress_cache_payload(
    *,
    status: str,
    metadata: dict[str, Any] | None,
    timestamp: int,
) -> dict[str, Any]:
    progress_data: dict[str, Any] = {
        "status": status,
        "timestamp": str(timestamp),
    }
    if metadata:
        progress_data.update(make_json_safe(metadata))
    return progress_data
