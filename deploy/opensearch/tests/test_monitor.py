"""The detector runs independently of workers and can report its own missing inputs."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

import httpx
import pytest


def loadMonitor() -> type:
    directory: Path = Path(__file__).parents[1]
    path: Path = directory / "monitor.py"
    assert path.is_file(), "Independent off-node monitor is not implemented"
    sys.path.insert(0, str(directory))
    specification = importlib.util.spec_from_file_location("native_monitor", path)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return cast(type, module.NativeMonitor)


def test_outage_detector_runs_even_when_worker_metrics_are_missing() -> None:
    monitor: type = loadMonitor()
    count: int = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        raise httpx.ConnectError("Injected unavailable node", request=request)

    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(respond)) as client:
        result = monitor.check(client, None, areMetricsFresh=False, previous={}, nowSeconds=1_000, repeatSeconds=900)
    assert count == 1
    assert {event["condition"] for event in result["events"]} >= {"native-unavailable", "operational-metrics-stale"}
    assert result["notification_delivery_verified"] is False
    assert result["mutates_job_or_backend"] is False


def test_fresh_healthy_metrics_clear_native_outage_without_sending_notifications() -> None:
    monitor: type = loadMonitor()
    metrics: dict[str, object] = {"indexing_oldest_seconds": 0, "pg_copy_oldest_seconds": 0, "latest_complete_snapshot_age_seconds": 0, "backup_incomplete": False}
    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"status": "green", "timed_out": False}))) as client:
        result = monitor.check(client, metrics, areMetricsFresh=True, previous={}, nowSeconds=1_000, repeatSeconds=900)
    assert result["events"] == []
    assert result["native_available"] is True


@pytest.mark.parametrize("resource", [None, [], "invalid"])
def test_malformed_resource_metrics_report_stale_source_instead_of_crashing(resource: object) -> None:
    monitor: type = loadMonitor()
    result = monitor.check(None, {"resource_pressure": resource}, areMetricsFresh=True,
        previous={}, nowSeconds=1_000, repeatSeconds=900)
    assert result["aggregate_metrics_fresh"] is False
    assert "operational-metrics-stale" in {event["condition"] for event in result["events"]}
