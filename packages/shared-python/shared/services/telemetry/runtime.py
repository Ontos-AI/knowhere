"""Runtime lifecycle helpers for anonymous self-hosted telemetry."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Protocol

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .client import TelemetryClient
from .config import TelemetryRuntimeConfig, TelemetrySettings, build_telemetry_config
from .events import (
    build_base_event_properties,
    build_instance_event_properties,
    uptime_seconds_to_bucket,
)
from .identity import get_or_create_installation_id

HealthProbe = Callable[[], Awaitable[bool]]


class DatabaseSessionFactory(Protocol):
    """Factory for app-owned async database sessions."""

    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]:
        """Return an async context manager yielding an AsyncSession."""
        raise NotImplementedError


class TelemetryHeartbeatSettings(TelemetrySettings, Protocol):
    TELEMETRY_AGGREGATE_INTERVAL_SECONDS: int


class SelfHostedHeartbeatTelemetryRunner:
    """Periodically emits real health heartbeats for anonymous telemetry."""

    def __init__(
        self,
        *,
        config: TelemetryRuntimeConfig,
        telemetry_client: TelemetryClient,
        settings: TelemetrySettings,
        interval_seconds: int,
        started_at_monotonic: float,
        postgres_probe: HealthProbe | None = None,
        redis_probe: HealthProbe | None = None,
    ) -> None:
        self._config = config
        self._telemetry_client = telemetry_client
        self._settings = settings
        self._interval_seconds = interval_seconds
        self._started_at_monotonic = started_at_monotonic
        self._postgres_probe = postgres_probe
        self._redis_probe = redis_probe
        self._task: asyncio.Task[None] | None = None

    async def emit_once(self) -> None:
        """Probe dependencies and capture one heartbeat event."""
        postgres_healthy = await _run_health_probe(self._postgres_probe, default=False)
        redis_healthy = await _run_health_probe(self._redis_probe, default=False)
        uptime_seconds = time.monotonic() - self._started_at_monotonic
        self._telemetry_client.capture(
            "self_hosted_instance_heartbeat",
            build_instance_event_properties(
                self._config,
                api_standalone_mode_enabled=self._settings.API_STANDALONE_MODE_ENABLED,
                billing_enabled=self._settings.BILLING_ENABLED,
                api_healthy=True,
                postgres_healthy=postgres_healthy,
                redis_healthy=redis_healthy,
                uptime_bucket=uptime_seconds_to_bucket(uptime_seconds),
            ),
        )

    def start(self) -> None:
        """Start the periodic heartbeat loop."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(),
            name="self-hosted-heartbeat-telemetry",
        )

    async def stop(self) -> None:
        """Stop the periodic heartbeat loop."""
        task = self._task
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self.emit_once()
            except Exception as exc:
                logger.warning(f"anonymous heartbeat telemetry failed: {exc}")


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


async def start_self_hosted_heartbeat_telemetry(
    settings: TelemetryHeartbeatSettings,
    *,
    telemetry_client: TelemetryClient | None,
    config: TelemetryRuntimeConfig | None,
    started_at_monotonic: float | None = None,
    postgres_probe: HealthProbe | None = None,
    redis_probe: HealthProbe | None = None,
) -> SelfHostedHeartbeatTelemetryRunner | None:
    """Start periodic real heartbeats when the base self-hosted client is active."""
    if telemetry_client is None or config is None:
        return None
    interval_seconds = max(settings.TELEMETRY_AGGREGATE_INTERVAL_SECONDS, 60)
    runner = SelfHostedHeartbeatTelemetryRunner(
        config=config,
        telemetry_client=telemetry_client,
        settings=settings,
        interval_seconds=interval_seconds,
        started_at_monotonic=(
            time.monotonic() if started_at_monotonic is None else started_at_monotonic
        ),
        postgres_probe=postgres_probe,
        redis_probe=redis_probe,
    )
    try:
        runner.start()
    except Exception as exc:
        logger.warning(f"anonymous heartbeat telemetry start failed: {exc}")
        return None
    logger.info("anonymous self-hosted heartbeat telemetry scheduled")
    return runner


async def stop_self_hosted_heartbeat_telemetry(
    runner: SelfHostedHeartbeatTelemetryRunner | None,
) -> None:
    """Stop heartbeat telemetry if it was started."""
    if runner is None:
        return
    await runner.stop()


async def stop_self_hosted_telemetry(
    telemetry_client: TelemetryClient | None,
    *,
    config: TelemetryRuntimeConfig | None = None,
) -> None:
    """Flush and stop anonymous self-hosted telemetry."""
    if telemetry_client is None:
        return
    shutdown_properties = (
        build_base_event_properties(config) if config is not None else {}
    )
    telemetry_client.capture("self_hosted_instance_shutdown", shutdown_properties)
    await telemetry_client.stop()


def build_postgres_health_probe(
    db_session_factory: DatabaseSessionFactory,
) -> HealthProbe:
    """Return a probe that runs SELECT 1 against Postgres."""

    async def _probe() -> bool:
        try:
            async with db_session_factory() as session:
                result = await session.execute(text("SELECT 1"))
                return result.scalar_one_or_none() == 1
        except Exception:
            return False

    return _probe


def build_redis_health_probe(redis_ping: Callable[[], Awaitable[bool]]) -> HealthProbe:
    """Return a probe that pings Redis through the provided callable."""

    async def _probe() -> bool:
        try:
            return bool(await redis_ping())
        except Exception:
            return False

    return _probe


async def _run_health_probe(
    probe: HealthProbe | None,
    *,
    default: bool,
) -> bool:
    if probe is None:
        return default
    try:
        return bool(await probe())
    except Exception:
        return False
