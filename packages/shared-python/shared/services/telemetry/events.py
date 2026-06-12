"""Anonymous telemetry event schema and safe property handling."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TypeAlias, cast

from .config import TelemetryRuntimeConfig

TelemetryPropertyValue: TypeAlias = str | int | float | bool | None
TelemetryProperties: TypeAlias = dict[str, TelemetryPropertyValue]

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
            "jobs_created_24h",
            "pages_processed_24h",
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
}


def get_allowed_telemetry_event_names() -> frozenset[str]:
    """Return event names that may be emitted by the telemetry client."""
    return frozenset(_EVENT_PROPERTY_NAMES.keys())


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
