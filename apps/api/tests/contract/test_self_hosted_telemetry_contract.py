"""Contract tests for anonymous self-hosted telemetry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
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
async def test_telemetry_client_sends_anonymous_posthog_batch(
    tmp_path: Path,
) -> None:
    sent_requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent_requests.append(request.read().decode("utf-8"))
        return httpx.Response(status_code=200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    telemetry_client = TelemetryClient(
        _build_config(tmp_path),
        http_client=http_client,
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
    assert len(sent_requests) == 1
    assert "phc_test_project_key" in str(sent_requests[0])
    assert "550e8400-e29b-41d4-a716-446655440000" in str(sent_requests[0])
    assert "$process_person_profile" in str(sent_requests[0])
    assert "private prompt" not in str(sent_requests[0])

    await http_client.aclose()


@pytest.mark.asyncio
async def test_telemetry_client_respects_batch_size(tmp_path: Path) -> None:
    sent_requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent_requests.append(request.read().decode("utf-8"))
        return httpx.Response(status_code=200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    config = _build_config(tmp_path)
    telemetry_client = TelemetryClient(
        replace(config, batch_size=2),
        http_client=http_client,
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

    assert len(sent_requests) == 2

    await http_client.aclose()


@pytest.mark.asyncio
async def test_telemetry_client_flush_before_start_does_not_deadlock(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    telemetry_client = TelemetryClient(
        _build_config(tmp_path),
        http_client=http_client,
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
    await http_client.aclose()


@pytest.mark.asyncio
async def test_telemetry_client_does_not_restart_after_stop(tmp_path: Path) -> None:
    sent_requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent_requests.append(request.read().decode("utf-8"))
        return httpx.Response(status_code=200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    telemetry_client = TelemetryClient(
        _build_config(tmp_path),
        http_client=http_client,
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

    assert len(sent_requests) == 1
    assert queued_after_stop is False

    await http_client.aclose()


@dataclass(frozen=True)
class _ConfigOverrides:
    installation_id: str = "550e8400-e29b-41d4-a716-446655440000"
    posthog_project_key: str = "phc_test_project_key"


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
