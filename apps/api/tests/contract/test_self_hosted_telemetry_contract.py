"""Contract tests for anonymous self-hosted telemetry."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.config.base import (
    DEFAULT_TELEMETRY_POSTHOG_PROJECT_KEY,
    BaseConfig,
)
from shared.services.telemetry.client import TelemetryClient
from shared.services.telemetry.config import TelemetryRuntimeConfig
from shared.services.telemetry.api_metrics import ApiRequestTelemetryMetrics
from shared.services.telemetry.aggregates import (
    TelemetryAggregateSettings,
    collect_self_hosted_aggregate_event_properties,
    start_self_hosted_aggregate_telemetry,
    stop_self_hosted_aggregate_telemetry,
)
from shared.services.telemetry.events import (
    SCHEMA_VERSION,
    build_base_event_properties,
    compute_success_rate,
    count_to_bucket,
    get_allowed_telemetry_event_names,
    normalize_client_name,
    normalize_document_type,
    normalize_source_type,
    sanitize_event_properties,
    uptime_seconds_to_bucket,
)
from shared.services.telemetry.identity import get_or_create_installation_id
from shared.services.telemetry.runtime import (
    SelfHostedHeartbeatTelemetryRunner,
    stop_self_hosted_telemetry,
)

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


def test_aggregate_event_names_are_allowed() -> None:
    assert {
        "self_hosted_usage_aggregate",
        "self_hosted_retrieval_aggregate",
        "self_hosted_worker_aggregate",
        "self_hosted_api_aggregate",
        "self_hosted_provider_aggregate",
        "self_hosted_document_type_aggregate",
        "self_hosted_client_aggregate",
    }.issubset(get_allowed_telemetry_event_names())


def test_normalize_document_type_and_client_name() -> None:
    assert normalize_document_type("report.PDF") == "pdf"
    assert normalize_document_type("photo.jpeg") == "image"
    assert normalize_document_type("notes.htm") == "html"
    assert normalize_document_type("secret.xyz") == "other"
    assert normalize_document_type(None) == "other"
    assert normalize_client_name("node-sdk") == "node-sdk"
    assert normalize_client_name("CLI") == "cli"
    assert normalize_client_name("custom-bot") == "other"
    assert normalize_source_type("direct_upload") == "file"
    assert normalize_source_type("url") == "url"
    assert normalize_source_type("demo") == "other"


def test_success_rate_and_count_buckets() -> None:
    assert compute_success_rate(9, 1) == 0.9
    assert compute_success_rate(0, 0) == 0.0
    assert count_to_bucket(0) == "0"
    assert count_to_bucket(7) == "1-10"
    assert count_to_bucket(50) == "11-100"
    assert count_to_bucket(101) == "100+"
    assert uptime_seconds_to_bucket(30) == "0m-5m"
    assert uptime_seconds_to_bucket(3600) == "1h-24h"


def test_usage_and_document_type_properties_strip_sensitive_values() -> None:
    usage_properties = sanitize_event_properties(
        "self_hosted_usage_aggregate",
        {
            "app_version": "1.2.3",
            "window_seconds": 86_400,
            "success_rate_24h": 0.9,
            "source_file_jobs_24h": 2,
            "email": "user@example.com",
            "document_name": "private.pdf",
            "source_file_name": "private.pdf",
        },
    )
    document_type_properties = sanitize_event_properties(
        "self_hosted_document_type_aggregate",
        {
            "document_type": "pdf",
            "jobs_created_24h": 1,
            "source_file_name": "private.pdf",
            "email": "user@example.com",
        },
    )
    client_properties = sanitize_event_properties(
        "self_hosted_client_aggregate",
        {
            "created_by_client": "cli",
            "jobs_created_24h": 1,
            "client_version": "9.9.9",
            "email": "user@example.com",
        },
    )

    assert usage_properties == {
        "app_version": "1.2.3",
        "window_seconds": 86_400,
        "success_rate_24h": 0.9,
        "source_file_jobs_24h": 2,
    }
    assert document_type_properties == {
        "document_type": "pdf",
        "jobs_created_24h": 1,
    }
    assert client_properties == {
        "created_by_client": "cli",
        "jobs_created_24h": 1,
    }


@pytest.mark.asyncio
async def test_heartbeat_emit_once_includes_health_and_uptime(
    tmp_path: Path,
) -> None:
    posthog_client = _FakePostHogClient()
    config = _build_config(tmp_path)
    telemetry_client = TelemetryClient(config, posthog_client=posthog_client)
    await telemetry_client.start()

    async def postgres_probe() -> bool:
        return True

    async def redis_probe() -> bool:
        return False

    runner = SelfHostedHeartbeatTelemetryRunner(
        config=config,
        telemetry_client=telemetry_client,
        settings=_HeartbeatSettings(),
        interval_seconds=60,
        started_at_monotonic=0.0,
        postgres_probe=postgres_probe,
        redis_probe=redis_probe,
    )
    await runner.emit_once()
    await telemetry_client.stop()

    assert len(posthog_client.captured_events) == 1
    captured = posthog_client.captured_events[0]
    assert captured.event_name == "self_hosted_instance_heartbeat"
    properties = cast(dict[str, object], captured.kwargs["properties"])
    assert properties["api_healthy"] is True
    assert properties["postgres_healthy"] is True
    assert properties["redis_healthy"] is False
    assert properties["uptime_bucket"] in {
        "0m-5m",
        "5m-1h",
        "1h-24h",
        "24h-7d",
        "7d+",
    }
    assert properties["schema_version"] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_shutdown_includes_base_event_properties(tmp_path: Path) -> None:
    posthog_client = _FakePostHogClient()
    config = _build_config(tmp_path)
    telemetry_client = TelemetryClient(config, posthog_client=posthog_client)
    await telemetry_client.start()

    await stop_self_hosted_telemetry(telemetry_client, config=config)

    assert len(posthog_client.captured_events) == 1
    captured = posthog_client.captured_events[0]
    assert captured.event_name == "self_hosted_instance_shutdown"
    assert captured.kwargs["properties"] == {
        **build_base_event_properties(config),
        "$process_person_profile": False,
    }


def test_usage_aggregate_allowlist_includes_v2_keys() -> None:
    properties = sanitize_event_properties(
        "self_hosted_usage_aggregate",
        {
            "success_rate_24h": 1.0,
            "job_duration_p95_seconds_24h": 12.5,
            "has_webhooks_24h": True,
            "has_retrieval_24h": False,
            "jobs_created_bucket": "1-10",
            "pages_processed_bucket": "0",
            "source_file_jobs_24h": 1,
            "source_url_jobs_24h": 0,
            "source_other_jobs_24h": 0,
            "filename": "secret.pdf",
        },
    )
    assert set(properties) == {
        "success_rate_24h",
        "job_duration_p95_seconds_24h",
        "has_webhooks_24h",
        "has_retrieval_24h",
        "jobs_created_bucket",
        "pages_processed_bucket",
        "source_file_jobs_24h",
        "source_url_jobs_24h",
        "source_other_jobs_24h",
    }


def test_self_hosted_telemetry_defaults_to_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEMETRY_ENABLED", raising=False)
    monkeypatch.delenv("TELEMETRY_POSTHOG_HOST", raising=False)
    monkeypatch.delenv("TELEMETRY_POSTHOG_PROJECT_KEY", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_POSTHOG_KEY", raising=False)

    config = BaseConfig(_env_file=None, TMP_PATH="/tmp/knowhere")

    assert config.TELEMETRY_ENABLED is True
    assert config.TELEMETRY_POSTHOG_HOST == "https://us.i.posthog.com"
    assert config.TELEMETRY_POSTHOG_PROJECT_KEY == DEFAULT_TELEMETRY_POSTHOG_PROJECT_KEY


def test_self_hosted_telemetry_env_can_disable_and_override_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("TELEMETRY_POSTHOG_PROJECT_KEY", "phc_test_override")
    monkeypatch.setenv("NEXT_PUBLIC_POSTHOG_KEY", "phc_next_public_should_not_win")

    config = BaseConfig(_env_file=None, TMP_PATH="/tmp/knowhere")

    assert config.TELEMETRY_ENABLED is False
    assert config.TELEMETRY_POSTHOG_PROJECT_KEY == "phc_test_override"


def test_aggregate_properties_strip_sensitive_values() -> None:
    properties = sanitize_event_properties(
        "self_hosted_usage_aggregate",
        {
            "app_version": "1.2.3",
            "window_seconds": 86_400,
            "total_jobs": 3,
            "total_users": 2,
            "email": "user@example.com",
            "document_name": "private.pdf",
            "query": "private retrieval query",
        },
    )

    assert properties == {
        "app_version": "1.2.3",
        "window_seconds": 86_400,
        "total_jobs": 3,
        "total_users": 2,
    }


def test_api_request_metrics_snapshot_resets_bucket() -> None:
    metrics = ApiRequestTelemetryMetrics()
    metrics.record(status_code=200, latency_ms=10)
    metrics.record(status_code=404, latency_ms=20)
    metrics.record(status_code=500, latency_ms=30)

    snapshot = metrics.snapshot_and_reset()
    empty_snapshot = metrics.snapshot_and_reset()

    assert snapshot.request_count == 3
    assert snapshot.status_2xx_count == 1
    assert snapshot.status_4xx_count == 1
    assert snapshot.status_5xx_count == 1
    assert snapshot.latency_avg_ms == 20
    assert empty_snapshot.request_count == 0


@pytest.mark.asyncio
async def test_api_aggregate_uses_interval_window_when_global_lock_unavailable(
    tmp_path: Path,
) -> None:
    metrics = ApiRequestTelemetryMetrics()
    metrics.record(status_code=200, latency_ms=12)

    properties = await collect_self_hosted_aggregate_event_properties(
        config=_build_config(tmp_path),
        db_session_factory=_build_lock_unavailable_session,
        api_metrics=metrics,
        window_seconds=86_400,
        api_window_seconds=300,
    )

    assert set(properties) == {"self_hosted_api_aggregate"}
    api_properties = properties["self_hosted_api_aggregate"]
    assert api_properties["window_seconds"] == 300
    assert api_properties["api_requests_total"] == 1
    assert api_properties["api_requests_2xx"] == 1


def test_telemetry_client_filters_posthog_sdk_properties_after_capture(
    tmp_path: Path,
) -> None:
    telemetry_client = TelemetryClient(_build_config(tmp_path))

    sanitized_message = telemetry_client._sanitize_posthog_message(
        {
            "event": "self_hosted_api_aggregate",
            "properties": {
                "app_version": "1.2.3",
                "api_requests_total": 1,
                "email": "user@example.com",
                "$context_tags": ["private-context"],
                "$geoip_disable": True,
                "$is_server": True,
                "$lib": "posthog-python",
                "$lib_version": "7.18.1",
                "$os": "Linux",
                "$os_version": "20.04",
                "$python_runtime": "CPython",
                "$python_version": "3.14.4",
            },
        }
    )

    assert sanitized_message is not None
    assert sanitized_message["properties"] == {
        "app_version": "1.2.3",
        "api_requests_total": 1,
        "$geoip_disable": True,
        "$is_server": True,
        "$lib": "posthog-python",
        "$lib_version": "7.18.1",
        "$os": "Linux",
        "$os_version": "20.04",
        "$python_runtime": "CPython",
        "$python_version": "3.14.4",
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
async def test_telemetry_client_sends_aggregate_events(tmp_path: Path) -> None:
    posthog_client = _FakePostHogClient()
    telemetry_client = TelemetryClient(
        _build_config(tmp_path),
        posthog_client=posthog_client,
    )

    await telemetry_client.start()
    queued = telemetry_client.capture(
        "self_hosted_api_aggregate",
        {
            "app_version": "1.2.3",
            "window_seconds": 86_400,
            "api_requests_total": 7,
            "api_requests_2xx": 6,
            "prompt": "private prompt",
        },
    )
    await telemetry_client.stop()

    assert queued is True
    assert len(posthog_client.captured_events) == 1
    captured_event = posthog_client.captured_events[0]
    assert captured_event.event_name == "self_hosted_api_aggregate"
    assert captured_event.kwargs["properties"] == {
        "app_version": "1.2.3",
        "window_seconds": 86_400,
        "api_requests_total": 7,
        "api_requests_2xx": 6,
        "$process_person_profile": False,
    }


@pytest.mark.asyncio
async def test_aggregate_telemetry_start_does_not_require_successful_snapshot(
    tmp_path: Path,
) -> None:
    posthog_client = _FakePostHogClient()
    telemetry_client = TelemetryClient(
        _build_config(tmp_path),
        posthog_client=posthog_client,
    )

    await telemetry_client.start()
    runner = await start_self_hosted_aggregate_telemetry(
        cast(TelemetryAggregateSettings, _AggregateSettings()),
        telemetry_client=telemetry_client,
        config=_build_config(tmp_path),
        db_session_factory=_build_failing_session,
        api_metrics=ApiRequestTelemetryMetrics(),
    )
    await stop_self_hosted_aggregate_telemetry(runner)
    await telemetry_client.stop()

    assert runner is not None
    assert posthog_client.captured_events == []


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


@dataclass(frozen=True)
class _AggregateSettings:
    TELEMETRY_AGGREGATE_INTERVAL_SECONDS: int = 60


@dataclass(frozen=True)
class _HeartbeatSettings:
    API_STANDALONE_MODE_ENABLED: bool = False
    BILLING_ENABLED: bool = False
    TELEMETRY_AGGREGATE_INTERVAL_SECONDS: int = 60


class _FailingSessionContext(AbstractAsyncContextManager[AsyncSession]):
    async def __aenter__(self) -> AsyncSession:
        raise RuntimeError("database unavailable")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None


@dataclass(frozen=True)
class _ScalarResult:
    value: object

    def scalar_one_or_none(self) -> object:
        return self.value


class _LockUnavailableSession:
    async def execute(
        self,
        statement: object,
        params: dict[str, Any] | None = None,
    ) -> _ScalarResult:
        assert "pg_try_advisory_lock" in str(statement)
        assert params is not None
        return _ScalarResult(False)


class _LockUnavailableSessionContext(AbstractAsyncContextManager[AsyncSession]):
    async def __aenter__(self) -> AsyncSession:
        return cast(AsyncSession, _LockUnavailableSession())

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None


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


def _build_failing_session() -> AbstractAsyncContextManager[AsyncSession]:
    return _FailingSessionContext()


def _build_lock_unavailable_session() -> AbstractAsyncContextManager[AsyncSession]:
    return _LockUnavailableSessionContext()


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
        schema_version=SCHEMA_VERSION,
    )
