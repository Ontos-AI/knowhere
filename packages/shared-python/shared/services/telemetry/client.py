"""Non-blocking PostHog telemetry client for anonymous self-hosted events."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

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


class TelemetryClient:
    """Bounded, asynchronous client for anonymous self-hosted telemetry."""

    def __init__(
        self,
        config: TelemetryRuntimeConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        queue_size: int = 100,
    ) -> None:
        self._config = config
        self._queue: asyncio.Queue[TelemetryEvent] = asyncio.Queue(maxsize=queue_size)
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._worker_task: asyncio.Task[None] | None = None
        self._is_closed = False
        self._allowed_event_names = get_allowed_telemetry_event_names()

    async def start(self) -> None:
        """Start the background sender if telemetry is configured."""
        if not self._config.is_ready or self._worker_task is not None:
            return

        if self._http_client is None:
            timeout = httpx.Timeout(self._config.request_timeout_seconds)
            self._http_client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
            )
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
            return

        while not self._queue.empty():
            telemetry_events = self._drain_batch()
            try:
                await self._send_batch(telemetry_events)
            finally:
                for _ in telemetry_events:
                    self._queue.task_done()

    async def stop(self) -> None:
        """Flush queued telemetry and close the background sender."""
        self._is_closed = True
        if self._config.is_ready:
            await self.flush()

        worker_task = self._worker_task
        self._worker_task = None
        if worker_task is not None:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task

        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
        self._http_client = None

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
        if not telemetry_events or self._http_client is None:
            return

        payload = self._build_batch_payload(telemetry_events)
        try:
            response = await self._http_client.post(
                self._config.batch_url,
                json=payload,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning(f"anonymous telemetry send failed: {exc}")

    def _build_batch_payload(self, telemetry_events: list[TelemetryEvent]) -> dict[str, Any]:
        return {
            "api_key": self._config.posthog_project_key,
            "historical_migration": False,
            "batch": [
                {
                    "event": telemetry_event.event_name,
                    "properties": {
                        **telemetry_event.properties,
                        "distinct_id": self._config.installation_id,
                        "$process_person_profile": False,
                    },
                    "timestamp": telemetry_event.timestamp.isoformat(),
                }
                for telemetry_event in telemetry_events
            ],
        }
