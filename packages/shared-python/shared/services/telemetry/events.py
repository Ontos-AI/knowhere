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


def _is_safe_property_value(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _read_bool_environment(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}
