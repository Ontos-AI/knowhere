"""Runtime lifecycle helpers for anonymous self-hosted telemetry."""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from .client import TelemetryClient
from .config import TelemetryRuntimeConfig, TelemetrySettings, build_telemetry_config
from .events import build_instance_event_properties
from .identity import get_or_create_installation_id


async def start_self_hosted_telemetry(
    settings: TelemetrySettings,
    *,
    service_name: str,
    api_healthy: bool,
    postgres_healthy: bool,
    redis_healthy: bool,
) -> tuple[TelemetryClient, TelemetryRuntimeConfig] | None:
    """Start anonymous self-hosted telemetry for the current service."""
    if not settings.TELEMETRY_ENABLED:
        logger.info("anonymous self-hosted telemetry disabled")
        return None

    try:
        installation_id = get_or_create_installation_id(
            explicit_installation_id=settings.TELEMETRY_INSTALLATION_ID,
            installation_id_path=Path(settings.TELEMETRY_INSTALLATION_ID_PATH),
        )
    except Exception as exc:
        logger.warning(f"anonymous self-hosted telemetry identity unavailable: {exc}")
        return None
    config = build_telemetry_config(
        settings,
        service_name=service_name,
        installation_id=installation_id,
    )
    if not config.is_ready:
        logger.warning(
            "anonymous self-hosted telemetry enabled but PostHog config is incomplete"
        )
        return None

    telemetry_client = TelemetryClient(config)
    await telemetry_client.start()

    base_properties = build_instance_event_properties(
        config,
        api_standalone_mode_enabled=settings.API_STANDALONE_MODE_ENABLED,
        billing_enabled=settings.BILLING_ENABLED,
    )
    telemetry_client.capture("self_hosted_instance_started", base_properties)
    telemetry_client.capture(
        "self_hosted_instance_heartbeat",
        build_instance_event_properties(
            config,
            api_standalone_mode_enabled=settings.API_STANDALONE_MODE_ENABLED,
            billing_enabled=settings.BILLING_ENABLED,
            api_healthy=api_healthy,
            postgres_healthy=postgres_healthy,
            redis_healthy=redis_healthy,
            uptime_bucket="0m-5m",
        ),
    )

    logger.info("anonymous self-hosted telemetry started")
    return telemetry_client, config


async def stop_self_hosted_telemetry(
    telemetry_client: TelemetryClient | None,
) -> None:
    """Flush and stop anonymous self-hosted telemetry."""
    if telemetry_client is None:
        return
    telemetry_client.capture("self_hosted_instance_shutdown", {})
    await telemetry_client.stop()
