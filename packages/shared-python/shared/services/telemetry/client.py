"""Non-blocking PostHog SDK client for anonymous self-hosted events."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from loguru import logger
from posthog import Posthog

from .config import TelemetryRuntimeConfig
from .events import (
    TelemetryProperties,
    get_allowed_telemetry_event_names,
    sanitize_event_properties,
)


@dataclass(frozen=True)
class TelemetryEvent:
    """Queued anonymous telemetry event."""

    event_name: str
    properties: TelemetryProperties
    timestamp: datetime


class PostHogTelemetryClient(Protocol):
    """Small PostHog SDK surface used by anonymous product telemetry."""

    def capture(self, event: str, **kwargs: Any) -> str | None:
        """Capture one event."""
        raise NotImplementedError

    def flush(self) -> None:
        """Flush the SDK queue."""
        raise NotImplementedError

    def shutdown(self) -> None:
        """Flush and stop the SDK client."""
        raise NotImplementedError


class TelemetryClient:
    """Bounded, asynchronous client for anonymous self-hosted telemetry."""

    def __init__(
        self,
        config: TelemetryRuntimeConfig,
        *,
        posthog_client: PostHogTelemetryClient | None = None,
        queue_size: int = 100,
    ) -> None:
        self._config = config
        self._queue: asyncio.Queue[TelemetryEvent] = asyncio.Queue(maxsize=queue_size)
        self._posthog_client = posthog_client
        self._owns_posthog_client = posthog_client is None
        self._queue_size = queue_size
        self._worker_task: asyncio.Task[None] | None = None
        self._is_closed = False
        self._allowed_event_names = get_allowed_telemetry_event_names()

    async def start(self) -> None:
        """Start the background sender if telemetry is configured."""
        if self._is_closed or not self._config.is_ready or self._worker_task is not None:
            return

        if self._posthog_client is None:
            self._posthog_client = self._create_posthog_client()
        self._worker_task = asyncio.create_task(self._run(), name="telemetry-client")

    def capture(
        self,
        event_name: str,
        properties: Mapping[str, object],
    ) -> bool:
        """Queue an anonymous telemetry event without blocking callers."""
        if self._is_closed or not self._config.is_ready:
            return False
        if event_name not in self._allowed_event_names:
            logger.warning(f"anonymous telemetry event rejected: {event_name}")
            return False

        telemetry_event = TelemetryEvent(
            event_name=event_name,
            properties=sanitize_event_properties(event_name, properties),
            timestamp=datetime.now(timezone.utc),
        )
        try:
            self._queue.put_nowait(telemetry_event)
        except asyncio.QueueFull:
            logger.warning("anonymous telemetry queue full; dropping event")
            return False
        return True

    async def flush(self) -> None:
        """Wait until queued telemetry has either sent or been dropped."""
        if not self._config.is_ready:
            return
        if self._worker_task is not None:
            await self._queue.join()
            await self._flush_posthog_client()
            return

        while not self._queue.empty():
            telemetry_events = self._drain_batch()
            try:
                await self._send_batch(telemetry_events)
            finally:
                for _ in telemetry_events:
                    self._queue.task_done()
        await self._flush_posthog_client()

    async def stop(self) -> None:
        """Flush queued telemetry and close the background sender."""
        self._is_closed = True
        if self._config.is_ready:
            await self.flush()

        worker_task = self._worker_task
        if worker_task is not None:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            self._worker_task = None

        if self._owns_posthog_client and self._posthog_client is not None:
            await self._shutdown_posthog_client()
        self._posthog_client = None

    async def _run(self) -> None:
        while True:
            telemetry_event = await self._queue.get()
            telemetry_events = [telemetry_event]
            telemetry_events.extend(
                self._drain_batch(self._config.batch_size - len(telemetry_events))
            )
            try:
                await self._send_batch(telemetry_events)
            finally:
                for _ in telemetry_events:
                    self._queue.task_done()

    def _drain_batch(self, max_count: int | None = None) -> list[TelemetryEvent]:
        telemetry_events: list[TelemetryEvent] = []
        batch_size = self._config.batch_size if max_count is None else max_count
        while len(telemetry_events) < batch_size:
            try:
                telemetry_events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return telemetry_events

    async def _send_batch(self, telemetry_events: list[TelemetryEvent]) -> None:
        posthog_client = self._posthog_client
        if not telemetry_events or posthog_client is None:
            return

        try:
            for telemetry_event in telemetry_events:
                posthog_client.capture(
                    telemetry_event.event_name,
                    distinct_id=self._config.installation_id,
                    properties={
                        **telemetry_event.properties,
                        "$process_person_profile": False,
                    },
                    timestamp=telemetry_event.timestamp,
                    disable_geoip=True,
                )
        except Exception as exc:
            logger.warning(f"anonymous telemetry send failed: {exc}")

    def _create_posthog_client(self) -> PostHogTelemetryClient:
        return Posthog(
            self._config.posthog_project_key,
            host=self._config.posthog_host,
            max_queue_size=self._queue_size,
            flush_at=self._config.batch_size,
            flush_interval=0.5,
            sync_mode=False,
            timeout=math.ceil(self._config.request_timeout_seconds),
            disable_geoip=True,
            enable_exception_autocapture=False,
            enable_local_evaluation=False,
        )

    async def _flush_posthog_client(self) -> None:
        posthog_client = self._posthog_client
        if posthog_client is None:
            return
        try:
            await asyncio.to_thread(posthog_client.flush)
        except Exception as exc:
            logger.warning(f"anonymous telemetry flush failed: {exc}")

    async def _shutdown_posthog_client(self) -> None:
        posthog_client = self._posthog_client
        if posthog_client is None:
            return
        try:
            await asyncio.to_thread(posthog_client.shutdown)
        except Exception as exc:
            logger.warning(f"anonymous telemetry shutdown failed: {exc}")
