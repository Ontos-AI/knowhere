"""Runtime configuration for anonymous self-hosted telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class TelemetrySettings(Protocol):
    """Subset of app settings required by anonymous telemetry."""

    TELEMETRY_ENABLED: bool
    TELEMETRY_POSTHOG_HOST: str
    TELEMETRY_POSTHOG_PROJECT_KEY: str
    TELEMETRY_INSTALLATION_ID: str
    TELEMETRY_INSTALLATION_ID_PATH: str
    TELEMETRY_BATCH_SIZE: int
    TELEMETRY_REQUEST_TIMEOUT_SECONDS: float
    TELEMETRY_DEPLOYMENT_MODE: str
    APP_VERSION: str
    ENVIRONMENT: str
    APP_ENV: str
    API_STANDALONE_MODE_ENABLED: bool
    BILLING_ENABLED: bool


@dataclass(frozen=True)
class TelemetryRuntimeConfig:
    """Resolved anonymous telemetry configuration."""

    enabled: bool
    posthog_host: str
    posthog_project_key: str
    installation_id: str
    installation_id_path: Path
    batch_size: int
    request_timeout_seconds: float
    deployment_mode: str
    app_version: str
    environment: str
    app_env: str
    service_name: str
    schema_version: str = "2026-06-telemetry-v1"

    @property
    def is_ready(self) -> bool:
        """Return whether outbound telemetry has enough config to send."""
        return (
            self.enabled
            and bool(self.posthog_host)
            and bool(self.posthog_project_key)
            and bool(self.installation_id)
        )


def build_telemetry_config(
    settings: TelemetrySettings,
    *,
    service_name: str,
    installation_id: str,
) -> TelemetryRuntimeConfig:
    """Build resolved anonymous telemetry config from app settings."""
    return TelemetryRuntimeConfig(
        enabled=settings.TELEMETRY_ENABLED,
        posthog_host=settings.TELEMETRY_POSTHOG_HOST,
        posthog_project_key=settings.TELEMETRY_POSTHOG_PROJECT_KEY.strip(),
        installation_id=installation_id.strip(),
        installation_id_path=Path(settings.TELEMETRY_INSTALLATION_ID_PATH),
        batch_size=settings.TELEMETRY_BATCH_SIZE,
        request_timeout_seconds=settings.TELEMETRY_REQUEST_TIMEOUT_SECONDS,
        deployment_mode=settings.TELEMETRY_DEPLOYMENT_MODE,
        app_version=settings.APP_VERSION,
        environment=settings.ENVIRONMENT,
        app_env=settings.APP_ENV,
        service_name=service_name,
    )
