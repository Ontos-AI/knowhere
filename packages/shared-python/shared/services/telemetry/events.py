"""Anonymous telemetry event schema and safe property handling."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TypeAlias, cast

from .config import SCHEMA_VERSION, TelemetryRuntimeConfig

TelemetryPropertyValue: TypeAlias = str | int | float | bool | None
TelemetryProperties: TypeAlias = dict[str, TelemetryPropertyValue]

DOCUMENT_TYPES = frozenset(
    {
        "pdf",
        "docx",
        "doc",
        "xlsx",
        "xls",
        "pptx",
        "ppt",
        "csv",
        "txt",
        "md",
        "html",
        "image",
        "other",
    }
)

CLIENT_NAMES = frozenset(
    {
        "cli",
        "node-sdk",
        "dashboard",
        "notebook",
        "mcp",
        "api",
        "other",
    }
)

SOURCE_TYPES = frozenset({"file", "url", "other"})

_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "tiff"})
_HTML_EXTENSIONS = frozenset({"html", "htm"})

_COUNT_BUCKETS = (
    (0, "0"),
    (10, "1-10"),
    (100, "11-100"),
)

_BASE_PROPERTY_NAMES = frozenset(
    {
        "app_env",
        "app_version",
        "api_standalone_mode_enabled",
        "billing_enabled",
        "deployment_mode",
        "environment",
        "rate_limit_enabled",
        "schema_version",
        "service_name",
    }
)

_AGGREGATE_PROPERTY_NAMES = frozenset(
    {
        "window_seconds",
    }
)

_POSTHOG_SDK_PROPERTY_NAMES = frozenset(
    {
        "$geoip_disable",
        "$is_server",
        "$lib",
        "$lib_version",
        "$os",
        "$os_distro",
        "$os_version",
        "$process_person_profile",
        "$python_runtime",
        "$python_version",
    }
)

_EVENT_PROPERTY_NAMES: dict[str, frozenset[str]] = {
    "self_hosted_instance_started": frozenset(),
    "self_hosted_instance_heartbeat": frozenset(
        {
            "api_healthy",
            "postgres_healthy",
            "redis_healthy",
            "uptime_bucket",
        }
    ),
    "self_hosted_instance_shutdown": frozenset(),
    "self_hosted_usage_aggregate": _AGGREGATE_PROPERTY_NAMES
    | frozenset(
        {
            "active_api_keys",
            "active_documents",
            "active_jobs",
            "completed_jobs_24h",
            "credits_charged_24h",
            "failed_jobs_24h",
            "has_retrieval_24h",
            "has_webhooks_24h",
            "job_duration_p95_seconds_24h",
            "jobs_created_24h",
            "jobs_created_bucket",
            "pages_processed_24h",
            "pages_processed_bucket",
            "source_file_jobs_24h",
            "source_other_jobs_24h",
            "source_url_jobs_24h",
            "success_rate_24h",
            "total_document_chunks",
            "total_documents",
            "total_job_chunks",
            "total_jobs",
            "total_users",
        }
    ),
    "self_hosted_retrieval_aggregate": _AGGREGATE_PROPERTY_NAMES
    | frozenset(
        {
            "retrieval_cache_hits_24h",
            "retrieval_errors_24h",
            "retrieval_latency_avg_ms_24h",
            "retrieval_latency_p95_ms_24h",
            "retrieval_result_count_24h",
            "retrieval_runs_24h",
            "retrieval_step_errors_24h",
            "retrieval_steps_24h",
            "retrieval_tokens_24h",
        }
    ),
    "self_hosted_worker_aggregate": _AGGREGATE_PROPERTY_NAMES
    | frozenset(
        {
            "job_duration_avg_seconds_24h",
            "jobs_converting",
            "jobs_done_24h",
            "jobs_failed_24h",
            "jobs_pending",
            "jobs_running",
            "jobs_waiting_file",
        }
    ),
    "self_hosted_api_aggregate": _AGGREGATE_PROPERTY_NAMES
    | frozenset(
        {
            "api_latency_avg_ms",
            "api_latency_p95_ms",
            "api_requests_2xx",
            "api_requests_3xx",
            "api_requests_4xx",
            "api_requests_5xx",
            "api_requests_total",
        }
    ),
    "self_hosted_provider_aggregate": _AGGREGATE_PROPERTY_NAMES
    | frozenset(
        {
            "parse_agent_errors_24h",
            "parse_agent_latency_avg_ms_24h",
            "parse_agent_runs_24h",
            "parse_agent_tokens_24h",
            "provider_error_count_24h",
            "provider_tokens_24h",
            "retrieval_model_count_24h",
            "retrieval_provider_tokens_24h",
            "webhook_deliveries_24h",
            "webhook_delivery_failures_24h",
        }
    ),
    "self_hosted_document_type_aggregate": _AGGREGATE_PROPERTY_NAMES
    | frozenset(
        {
            "document_type",
            "jobs_created_24h",
            "jobs_done_24h",
            "jobs_failed_24h",
            "pages_processed_24h",
            "success_rate_24h",
        }
    ),
    "self_hosted_client_aggregate": _AGGREGATE_PROPERTY_NAMES
    | frozenset(
        {
            "created_by_client",
            "jobs_created_24h",
            "jobs_done_24h",
            "jobs_failed_24h",
            "success_rate_24h",
        }
    ),
}


def get_allowed_telemetry_event_names() -> frozenset[str]:
    """Return event names that may be emitted by the telemetry client."""
    return frozenset(_EVENT_PROPERTY_NAMES.keys())


def normalize_document_type(extension_or_filename: str | None) -> str:
    """Map a filename or extension to an allowlisted document_type."""
    if not extension_or_filename:
        return "other"
    raw = extension_or_filename.strip().lower()
    if not raw:
        return "other"
    # Accept either "pdf" or "report.pdf" / "path/report.PDF".
    if "/" in raw or "\\" in raw or "." in raw:
        suffix = PurePosixPath(raw.replace("\\", "/")).suffix
        extension = suffix.lstrip(".")
    else:
        extension = raw.lstrip(".")
    if not extension:
        return "other"
    if extension in _IMAGE_EXTENSIONS:
        return "image"
    if extension in _HTML_EXTENSIONS:
        return "html"
    if extension in DOCUMENT_TYPES:
        return extension
    return "other"


def normalize_client_name(raw: str | None) -> str:
    """Map a created_by_client value to an allowlisted client name."""
    if raw is None:
        return "other"
    normalized = raw.strip().lower()
    if normalized in CLIENT_NAMES:
        return normalized
    return "other"


def normalize_source_type(raw: str | None) -> str:
    """Map a job source_type value to an allowlisted source_type."""
    if raw is None:
        return "other"
    normalized = raw.strip().lower()
    if normalized == "direct_upload":
        return "file"
    if normalized in SOURCE_TYPES:
        return normalized
    return "other"


def compute_success_rate(done: int, failed: int) -> float:
    """Return done / (done + failed) as a float in 0–1."""
    terminal = max(done, 0) + max(failed, 0)
    if terminal == 0:
        return 0.0
    return max(done, 0) / terminal


def count_to_bucket(count: int) -> str:
    """Bucket a non-negative count into 0|1-10|11-100|100+."""
    value = max(count, 0)
    for upper_bound, label in _COUNT_BUCKETS:
        if value <= upper_bound:
            return label
    return "100+"


def uptime_seconds_to_bucket(uptime_seconds: float) -> str:
    """Bucket process uptime for heartbeat events."""
    seconds = max(uptime_seconds, 0.0)
    if seconds < 5 * 60:
        return "0m-5m"
    if seconds < 60 * 60:
        return "5m-1h"
    if seconds < 24 * 60 * 60:
        return "1h-24h"
    if seconds < 7 * 24 * 60 * 60:
        return "24h-7d"
    return "7d+"


def build_instance_event_properties(
    config: TelemetryRuntimeConfig,
    *,
    api_standalone_mode_enabled: bool,
    billing_enabled: bool,
    api_healthy: bool | None = None,
    postgres_healthy: bool | None = None,
    redis_healthy: bool | None = None,
    uptime_bucket: str | None = None,
) -> TelemetryProperties:
    """Build safe common properties for self-hosted instance events."""
    properties: TelemetryProperties = {
        "app_env": config.app_env,
        "app_version": config.app_version,
        "api_standalone_mode_enabled": api_standalone_mode_enabled,
        "billing_enabled": billing_enabled,
        "deployment_mode": config.deployment_mode,
        "environment": config.environment,
        "rate_limit_enabled": _read_bool_environment("RATE_LIMIT_ENABLED", True),
        "schema_version": config.schema_version,
        "service_name": config.service_name,
    }
    if api_healthy is not None:
        properties["api_healthy"] = api_healthy
    if postgres_healthy is not None:
        properties["postgres_healthy"] = postgres_healthy
    if redis_healthy is not None:
        properties["redis_healthy"] = redis_healthy
    if uptime_bucket is not None:
        properties["uptime_bucket"] = uptime_bucket
    return properties


def build_base_event_properties(config: TelemetryRuntimeConfig) -> TelemetryProperties:
    """Build safe common properties for all self-hosted telemetry events."""
    return {
        "app_env": config.app_env,
        "app_version": config.app_version,
        "api_standalone_mode_enabled": _read_bool_environment(
            "API_STANDALONE_MODE_ENABLED",
            False,
        ),
        "billing_enabled": _read_bool_environment("BILLING_ENABLED", False),
        "deployment_mode": config.deployment_mode,
        "environment": config.environment,
        "rate_limit_enabled": _read_bool_environment("RATE_LIMIT_ENABLED", True),
        "schema_version": config.schema_version,
        "service_name": config.service_name,
    }


def sanitize_event_properties(
    event_name: str,
    properties: Mapping[str, object],
) -> TelemetryProperties:
    """Strip unknown or non-scalar properties before outbound telemetry."""
    allowed_property_names = _BASE_PROPERTY_NAMES | _EVENT_PROPERTY_NAMES[event_name]
    sanitized_properties: TelemetryProperties = {}
    for property_name, property_value in properties.items():
        if property_name not in allowed_property_names:
            continue
        if _is_safe_property_value(property_value):
            sanitized_properties[property_name] = cast(
                TelemetryPropertyValue,
                property_value,
            )
    return sanitized_properties


def sanitize_posthog_event_properties(
    event_name: str,
    properties: Mapping[str, object],
) -> TelemetryProperties:
    """Strip unknown properties after the PostHog SDK adds system metadata."""
    sanitized_properties = sanitize_event_properties(event_name, properties)
    for property_name in _POSTHOG_SDK_PROPERTY_NAMES:
        if property_name not in properties:
            continue
        property_value = properties.get(property_name)
        if _is_safe_property_value(property_value):
            sanitized_properties[property_name] = cast(
                TelemetryPropertyValue,
                property_value,
            )
    return sanitized_properties


def _is_safe_property_value(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _read_bool_environment(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}
