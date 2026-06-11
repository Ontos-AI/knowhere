"""Contract tests for anonymous self-hosted telemetry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from shared.services.telemetry.client import TelemetryClient
from shared.services.telemetry.config import TelemetryRuntimeConfig
from shared.services.telemetry.events import sanitize_event_properties
from shared.services.telemetry.identity import get_or_create_installation_id


def test_installation_id_is_generated_once(tmp_path: Path) -> None:
    installation_id_path = tmp_path / "telemetry-installation-id"

    first_installation_id = get_or_create_installation_id(
        explicit_installation_id="",
        installation_id_path=installation_id_path,
    )
    second_installation_id = get_or_create_installation_id(
        explicit_installation_id="",
        installation_id_path=installation_id_path,
    )

    assert first_installation_id == second_installation_id
    assert installation_id_path.read_text(encoding="utf-8").strip() == (
        first_installation_id
    )


def test_explicit_installation_id_does_not_write_file(tmp_path: Path) -> None:
    installation_id_path = tmp_path / "telemetry-installation-id"
    explicit_installation_id = "550e8400-e29b-41d4-a716-446655440000"

    installation_id = get_or_create_installation_id(
        explicit_installation_id=explicit_installation_id,
        installation_id_path=installation_id_path,
    )

    assert installation_id == explicit_installation_id
    assert not installation_id_path.exists()


def test_explicit_installation_id_must_be_uuid(tmp_path: Path) -> None:
    installation_id_path = tmp_path / "telemetry-installation-id"

    with pytest.raises(ValueError, match="must be a UUID"):
        get_or_create_installation_id(
            explicit_installation_id="customer@example.com",
            installation_id_path=installation_id_path,
        )


def test_telemetry_properties_strip_unknown_and_non_scalar_values() -> None:
    properties = sanitize_event_properties(
        "self_hosted_instance_heartbeat",
        {
            "app_version": "1.2.3",
            "api_healthy": True,
            "email": "user@example.com",
            "prompt": "private prompt",
            "nested": {"unsafe": True},
            "document_id": "doc_123",
        },
    )

    assert properties == {
        "app_version": "1.2.3",
        "api_healthy": True,
    }


@pytest.mark.asyncio
async def test_telemetry_client_sends_anonymous_posthog_capture(
    tmp_path: Path,
) -> None:
    posthog_client = _FakePostHogClient()
    telemetry_client = TelemetryClient(
        _build_config(tmp_path),
        posthog_client=posthog_client,
    )

    await telemetry_client.start()
    queued = telemetry_client.capture(
        "self_hosted_instance_heartbeat",
        {
            "app_version": "1.2.3",
            "api_healthy": True,
            "prompt": "private prompt",
        },
    )
    await telemetry_client.stop()

    assert queued is True
    assert len(posthog_client.captured_events) == 1
    captured_event = posthog_client.captured_events[0]
    assert captured_event.event_name == "self_hosted_instance_heartbeat"
    assert captured_event.kwargs["distinct_id"] == (
        "550e8400-e29b-41d4-a716-446655440000"
    )
    assert captured_event.kwargs["disable_geoip"] is True
    assert captured_event.kwargs["properties"] == {
        "app_version": "1.2.3",
        "api_healthy": True,
        "$process_person_profile": False,
    }
    assert posthog_client.flush_count == 1


@pytest.mark.asyncio
async def test_telemetry_client_respects_batch_size(tmp_path: Path) -> None:
    posthog_client = _FakePostHogClient()
    config = _build_config(tmp_path)
    telemetry_client = TelemetryClient(
        replace(config, batch_size=2),
        posthog_client=posthog_client,
    )

    await telemetry_client.start()
    for index in range(3):
        telemetry_client.capture(
            "self_hosted_instance_heartbeat",
            {
                "app_version": f"1.2.{index}",
            },
        )
    await telemetry_client.stop()

    assert [event.event_name for event in posthog_client.captured_events] == [
        "self_hosted_instance_heartbeat",
        "self_hosted_instance_heartbeat",
        "self_hosted_instance_heartbeat",
    ]
    assert posthog_client.flush_count == 1


@pytest.mark.asyncio
async def test_telemetry_client_flush_before_start_does_not_deadlock(
    tmp_path: Path,
) -> None:
    posthog_client = _FakePostHogClient()
    telemetry_client = TelemetryClient(
        _build_config(tmp_path),
        posthog_client=posthog_client,
    )

    telemetry_client.capture(
        "self_hosted_instance_heartbeat",
        {
            "app_version": "1.2.3",
        },
    )
    await telemetry_client.flush()
    await telemetry_client.start()
    telemetry_client.capture(
        "self_hosted_instance_heartbeat",
        {
            "app_version": "1.2.4",
        },
    )

    await asyncio.wait_for(telemetry_client.stop(), timeout=1.0)
    assert len(posthog_client.captured_events) == 2


@pytest.mark.asyncio
async def test_telemetry_client_does_not_restart_after_stop(tmp_path: Path) -> None:
    posthog_client = _FakePostHogClient()
    telemetry_client = TelemetryClient(
        _build_config(tmp_path),
        posthog_client=posthog_client,
    )

    await telemetry_client.start()
    telemetry_client.capture(
        "self_hosted_instance_heartbeat",
        {
            "app_version": "1.2.3",
        },
    )
    await telemetry_client.stop()
    await telemetry_client.start()
    queued_after_stop = telemetry_client.capture(
        "self_hosted_instance_heartbeat",
        {
            "app_version": "1.2.4",
        },
    )

    assert len(posthog_client.captured_events) == 1
    assert queued_after_stop is False


@dataclass(frozen=True)
class _ConfigOverrides:
    installation_id: str = "550e8400-e29b-41d4-a716-446655440000"
    posthog_project_key: str = "phc_test_project_key"


@dataclass(frozen=True)
class _CapturedPostHogEvent:
    event_name: str
    kwargs: dict[str, object]


class _FakePostHogClient:
    def __init__(self) -> None:
        self.captured_events: list[_CapturedPostHogEvent] = []
        self.flush_count = 0
        self.shutdown_count = 0

    def capture(self, event: str, **kwargs: object) -> str:
        self.captured_events.append(
            _CapturedPostHogEvent(event_name=event, kwargs=kwargs)
        )
        return "fake-posthog-event-id"

    def flush(self) -> None:
        self.flush_count += 1

    def shutdown(self) -> None:
        self.shutdown_count += 1


def _build_config(
    tmp_path: Path,
    overrides: _ConfigOverrides = _ConfigOverrides(),
) -> TelemetryRuntimeConfig:
    return TelemetryRuntimeConfig(
        enabled=True,
        posthog_host="https://us.i.posthog.com",
        posthog_project_key=overrides.posthog_project_key,
        installation_id=overrides.installation_id,
        installation_id_path=tmp_path / "telemetry-installation-id",
        batch_size=20,
        request_timeout_seconds=2.0,
        deployment_mode="self_hosted",
        app_version="1.2.3",
        environment="production",
        app_env="production",
        service_name="knowhere-api",
    )
