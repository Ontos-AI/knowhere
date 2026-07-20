"""Anonymous self-hosted telemetry helpers."""

from .client import TelemetryClient
from .config import SCHEMA_VERSION, TelemetryRuntimeConfig, build_telemetry_config
from .events import (
    build_base_event_properties,
    build_instance_event_properties,
    get_allowed_telemetry_event_names,
    normalize_client_name,
    normalize_document_type,
    normalize_source_type,
)
from .identity import get_or_create_installation_id

__all__ = [
    "SCHEMA_VERSION",
    "TelemetryClient",
    "TelemetryRuntimeConfig",
    "build_telemetry_config",
    "build_base_event_properties",
    "build_instance_event_properties",
    "get_allowed_telemetry_event_names",
    "get_or_create_installation_id",
    "normalize_client_name",
    "normalize_document_type",
    "normalize_source_type",
]
