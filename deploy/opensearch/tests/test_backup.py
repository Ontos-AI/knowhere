"""Hourly submission and native retention use the supplied private transport."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

import httpx
import pytest

from test_operations import createInventory, createShardStatus, createSnapshot


def loadBackup() -> type:
    directory: Path = Path(__file__).parents[1]
    path: Path = directory / "backup.py"
    assert path.is_file(), "Hourly native backup coordinator is not implemented"
    sys.path.insert(0, str(directory))
    specification = importlib.util.spec_from_file_location("native_backup", path)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return cast(type, module.BackupOperations)


@pytest.mark.parametrize("extraIndex", [None, "knowhere-lexical-v1-published-manifests", "knowhere-lexical-v1-published-manifests-extra"])
def test_submits_closed_catalog_once_and_preserves_pending_identity(extraIndex: str | None) -> None:
    backup: type = loadBackup()
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.startswith("/_cat/"):
            rows: list[dict[str, object]] = [{"index": row["name"], "uuid": row["uuid"],
                "status": "close" if row["state"] == "closed" else "open", "pri": "1"} for row in createInventory()]
            if extraIndex is not None:
                rows.append({"index": extraIndex, "uuid": "extra", "status": "open", "pri": "1"})
            return httpx.Response(200, json=rows)
        return httpx.Response(200, json={"accepted": True})

    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(respond)) as client:
        def run() -> dict[str, object]:
            return backup.runCycle(client, indexPrefix="knowhere-lexical-v1", inventory=createInventory(),
                boundarySha256="d" * 64, state={}, nowMillis=1_000)

        if extraIndex == "knowhere-lexical-v1-published-manifests-extra":
            with pytest.raises(ValueError, match="Expected application catalog differs"):
                run()
            assert [request.method for request in calls] == ["GET"]
            return
        result: dict[str, object] = run()
    state: object = result["state"]
    assert isinstance(state, dict)
    assert state["pending"]["snapshot"] == "hourly-1000"
    assert state["points"] == []
    assert [request.method for request in calls] == ["GET", "PUT"]
    assert b"knowhere-lexical-v1-cold" in calls[1].content
    assert b"published-manifests" not in calls[1].content
    assert result["coverage_verified"] is False


def test_pending_snapshot_is_polled_without_second_submission_or_opening_cold_indexes() -> None:
    backup: type = loadBackup()
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.method + " " + request.url.path)
        return httpx.Response(200, json={"snapshots": [{"snapshot": "hourly-1000", "state": "IN_PROGRESS"}]})

    state: dict[str, object] = {"points": [], "pending": {"snapshot": "hourly-1000", "inventory": createInventory(), "boundary_sha256": "d" * 64}}
    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(respond)) as client:
        result = backup.runCycle(client, indexPrefix="knowhere-lexical-v1", inventory=createInventory(), boundarySha256="d" * 64, state=state, nowMillis=2_000)
    assert calls == ["GET /_snapshot/knowhere-full/hourly-1000"]
    assert result["waiting_for_completion"] is True
    assert result["state"]["pending"]["snapshot"] == "hourly-1000"


def test_native_retention_rechecks_three_current_verified_uuids_before_deleting() -> None:
    backup: type = loadBackup()
    calls: list[str] = []
    points: list[dict[str, object]] = [{"snapshot": f"old-{index}", "snapshot_uuid": f"uuid-{index}", "state": "SUCCESS", "coverage_verified": True, "completed_at_millis": index * 86_400_000} for index in range(1, 5)]

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.method + " " + request.url.path)
        return httpx.Response(200, json={"snapshots": [{"snapshot": point["snapshot"], "uuid": point["snapshot_uuid"], "state": "SUCCESS"} for point in points]} if request.method == "GET" else {"acknowledged": True})

    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(respond)) as client:
        result = backup.applyRetention(client, points, nowMillis=30 * 86_400_000)
    assert calls == ["GET /_snapshot/knowhere-full/_all", "DELETE /_snapshot/knowhere-full/old-1"]
    assert result["deleted"] == ["old-1"]


def test_minute_polling_keeps_hourly_submission_and_submits_when_due_after_completion() -> None:
    backup: type = loadBackup()
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.method + " " + request.url.path)
        if request.url.path.startswith("/_cat/"):
            return httpx.Response(200, json=[{"index": row["name"], "uuid": row["uuid"], "status": "close" if row["state"] == "closed" else "open", "pri": "1"} for row in createInventory()])
        if request.url.path.endswith("/_status"):
            return httpx.Response(200, json=createShardStatus())
        if request.method == "GET":
            return httpx.Response(200, json={"snapshots": [createSnapshot()]})
        return httpx.Response(200, json={"accepted": True})

    state: dict[str, object] = {"points": [], "pending": {"snapshot": "hourly-test", "inventory": createInventory(),
        "intent_at_millis": 1_000, "boundary_captured_at_millis": 500, "boundary_sha256": "d" * 64}}
    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(respond)) as client:
        completed = backup.runCycle(client, indexPrefix="knowhere-lexical-v1", inventory=createInventory(), boundarySha256="d" * 64,
            state=state, nowMillis=61_000, verifiedBoundarySha256="d" * 64, boundaryCapturedAtMillis=500)
        assert completed["coverage_verified"] is True
        skipped = backup.runCycle(client, indexPrefix="knowhere-lexical-v1", inventory=createInventory(), boundarySha256="d" * 64,
            state=completed["state"], nowMillis=121_000, boundaryCapturedAtMillis=500)
        assert skipped["not_due"] is True
        due = backup.runCycle(client, indexPrefix="knowhere-lexical-v1", inventory=createInventory(), boundarySha256="d" * 64,
            state=completed["state"], nowMillis=3_601_000, boundaryCapturedAtMillis=500)
        assert due["submitted"] is True
    assert sum(call.startswith("PUT ") for call in calls) == 1


def test_overdue_completion_can_submit_next_hourly_snapshot_in_same_cycle() -> None:
    backup: type = loadBackup()
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.url.path.startswith("/_cat/"):
            return httpx.Response(200, json=[{"index": row["name"], "uuid": row["uuid"], "status": "close" if row["state"] == "closed" else "open", "pri": "1"} for row in createInventory()])
        if request.url.path.endswith("/_status"):
            return httpx.Response(200, json=createShardStatus())
        return httpx.Response(200, json={"snapshots": [createSnapshot()]} if request.method == "GET" else {"accepted": True})

    state: dict[str, object] = {"points": [], "pending": {"snapshot": "hourly-test", "inventory": createInventory(), "intent_at_millis": 1_000,
        "boundary_captured_at_millis": 500, "boundary_sha256": "d" * 64}}
    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(respond)) as client:
        result = backup.runCycle(client, indexPrefix="knowhere-lexical-v1", inventory=createInventory(), boundarySha256="d" * 64,
            state=state, nowMillis=3_601_000, verifiedBoundarySha256="d" * 64, boundaryCapturedAtMillis=3_600_000)
    assert result["completed"] is True and result["submitted"] is True
    assert calls[-1] == "PUT"
