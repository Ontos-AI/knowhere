"""Restore callbacks preserve admission on ambiguous remote outcomes."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

import httpx
import pytest


def loadRecovery() -> type:
    directory: Path = Path(__file__).parents[1]
    path: Path = directory / "recovery.py"
    assert path.is_file(), "Guarded native recovery adapter is not implemented"
    sys.path.insert(0, str(directory))
    specification = importlib.util.spec_from_file_location("native_recovery", path)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return cast(type, module.RecoveryOperations)


def createRestoreInventory() -> dict[str, object]:
    return {"version": 1, "plan": {
        "expected_backend": "opensearch", "expected_old_cluster_uuid": "old-cluster", "expected_old_incarnation": "old-node",
        "target_cluster_uuid": "new-cluster", "target_incarnation": "new-node", "writers_fenced": True,
        "snapshot": {"repository": "knowhere-full", "name": "approved-hourly", "uuid": "snapshot-one", "manifest_sha256": "a" * 64},
        "indices": [{"owner_id": "owner", "namespace": "private", "corpus_kind": "private", "index_name": "knowhere-lexical-v1-one", "old_index_uuid": "old-uuid", "expected_state": "closed", "expected_operation_id": None,
            "builds": [{"build_id": "build-one", "document_id": "document-one", "revision_id": "revision-one", "unit_count": 10}]}],
    }, "attestation": None}


def createReservation(recovery: type, inventory: object, *, verb: str, status: str) -> dict[str, object]:
    return {"mode": verb, "status": status, "epoch": 4, "operation_id": "operation-one",
        "inventory_sha256": recovery.inventoryDigest(inventory), "execute": True, "errors": [], "cap": 400,
        "cluster_uuid": "new-cluster", "sourceHash": "c" * 64,
        "charged_after": 1,
        "indices": [{"index_name": "knowhere-lexical-v1-one", "reservation_id": "close-reservation-one", "restored_index_uuid": "new-uuid", "coverage_sha256": "e" * 64}]}


def createIdentityResponse() -> httpx.Response:
    return httpx.Response(200, json={"cluster_uuid": "new-cluster", "nodes": {"node-one": {"ephemeral_id": "new-node"}}})


def test_restore_reserves_before_native_http_and_keeps_reservation_on_unknown_reply() -> None:
    recovery: type = loadRecovery()
    calls: list[str] = []

    def reserve() -> dict[str, object]:
        calls.append("reserve")
        return createReservation(recovery, createRestoreInventory(), verb="reserve-restore", status="reserved")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/_cluster/state/"):
            return createIdentityResponse()
        calls.append("native-restore")
        raise httpx.ReadTimeout("Injected ambiguous restore reply", request=request)

    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(respond)) as client:
        result = recovery.submit(client, createRestoreInventory(), epoch=4, operationId="operation-one", reserve=reserve)
    assert calls == ["reserve", "native-restore"]
    assert result["native_outcome"] == "unknown"
    assert result["reservation_held"] is True
    assert result["reopen_traffic"] is False


def test_adapter_target_identity_must_match_before_native_restore_even_after_admission() -> None:
    recovery: type = loadRecovery()
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"cluster_uuid": "different-cluster", "nodes": {"node-one": {"ephemeral_id": "new-node"}}})

    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(respond)) as client:
        result = recovery.submit(client, createRestoreInventory(), epoch=4, operationId="operation-one",
            reserve=lambda: createReservation(recovery, createRestoreInventory(), verb="reserve-restore", status="reserved"))
    assert calls == ["GET"]
    assert result["native_outcome"] == "unknown"
    assert result["reservation_held"] is True


def test_fresh_restore_uses_attested_new_uuid_instead_of_assuming_snapshot_uuid_survives() -> None:
    recovery: type = loadRecovery()
    inventory: dict[str, object] = createRestoreInventory()
    inventory["attestation"] = {"snapshot_uuid": "snapshot-one", "manifest_sha256": "a" * 64,
        "identity_mapping": [{"index_name": "knowhere-lexical-v1-one", "old_index_uuid": "old-uuid", "restored_index_uuid": "new-uuid"}]}
    mapping = recovery.validateAttestation(inventory)
    assert mapping == {"knowhere-lexical-v1-one": "new-uuid"}
    inventory["attestation"]["snapshot_uuid"] = "unapproved-snapshot"
    with pytest.raises(ValueError):
        recovery.validateAttestation(inventory)


def test_refuses_native_restore_when_gate_or_admission_is_missing() -> None:
    recovery: type = loadRecovery()
    count: int = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(200, json={"accepted": True})

    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError):
            recovery.submit(client, createRestoreInventory(), epoch=4, operationId="operation-one", reserve=lambda: {"status": "unknown"})
    assert count == 0


@pytest.mark.parametrize("outcome", ["unknown", "unknown-close", "admission-intent", None])
def test_unknown_issuer_outcome_cannot_authorize_verify_or_opposite_close(outcome: str | None) -> None:
    recovery: type = loadRecovery()
    with pytest.raises(ValueError, match="reconciliation"):
        recovery.assertFinishableIntent({"native_outcome": outcome})
    recovery.assertFinishableIntent({"native_outcome": "submitted", "reservation_held": True})


def test_refuses_authoritative_capacity_overflow_without_adding_fences_twice() -> None:
    recovery: type = loadRecovery()
    inventory: dict[str, object] = createRestoreInventory()
    reservation: dict[str, object] = createReservation(recovery, inventory, verb="reserve-restore", status="reserved")
    reservation["charged_after"] = 401
    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(lambda _request: pytest.fail("No native request is allowed over capacity"))) as client:
        with pytest.raises(ValueError):
            recovery.submit(client, inventory, epoch=4, operationId="operation-one", reserve=lambda: reservation)


def test_closes_only_under_verified_reservation_and_releases_after_acknowledged_close() -> None:
    recovery: type = loadRecovery()
    inventory: dict[str, object] = createRestoreInventory()
    inventory["attestation"] = {"snapshot_uuid": "snapshot-one", "manifest_sha256": "a" * 64,
        "identity_mapping": [{"index_name": "knowhere-lexical-v1-one", "old_index_uuid": "old-uuid", "restored_index_uuid": "new-uuid"}]}
    calls: list[str] = []

    def verify() -> dict[str, object]:
        calls.append("verify")
        return createReservation(recovery, inventory, verb="verify-restore", status="verified")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/_cluster/state/"):
            return createIdentityResponse()
        calls.append("close")
        assert request.headers["X-Opaque-Id"] == "close-reservation-one"
        return httpx.Response(200, json={"acknowledged": True, "shards_acknowledged": True, "indices": {"knowhere-lexical-v1-one": {"closed": True}}})

    def release(updated: dict[str, object], _verified: dict[str, object]) -> dict[str, object]:
        calls.append("release")
        assert updated["attestation"]["identity_mapping"][0]["close_acknowledged"] is True
        assert updated["attestation"]["identity_mapping"][0]["close_operation_id"] == "close-reservation-one"
        return createReservation(recovery, inventory, verb="release-restore", status="released")

    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(respond)) as client:
        result = recovery.finish(client, inventory, epoch=4, operationId="operation-one", verify=verify, release=release)
    assert calls == ["verify", "close", "release"]
    assert result["reservation_held"] is False
    assert result["reopen_traffic"] is False


@pytest.mark.parametrize("state", ["open", "closed"])
def test_live_remote_uses_held_reservation_and_needs_no_snapshot_catalog(state: str) -> None:
    recovery: type = loadRecovery()
    inventory: dict[str, object] = createRestoreInventory()
    del inventory["plan"]["snapshot"]
    inventory["plan"]["recovery_source"] = "live_remote"
    inventory["plan"]["remote_state"] = {"evidence_sha256": "f" * 64, "indices": [{"index_name": "knowhere-lexical-v1-one", "index_uuid": "old-uuid"}]}
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/_cluster/state/"):
            return createIdentityResponse()
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[{"index": "knowhere-lexical-v1-one", "uuid": "old-uuid", "status": "close" if state == "closed" else "open", "health": "red"}])
        assert request.headers["X-Opaque-Id"] == "close-reservation-one"
        assert request.url.path == "/_remotestore/_restore"
        assert request.url.params["restore_all_shards"] == ("true" if state == "closed" else "false")
        return httpx.Response(202, json={"accepted": True})

    with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(respond)) as client:
        result = recovery.submit(client, inventory, epoch=4, operationId="operation-one",
            reserve=lambda: createReservation(recovery, inventory, verb="reserve-restore", status="reserved"))
    assert [request.method for request in calls] == ["GET", "POST"]
    assert result["native_outcome"] == "submitted"
    attestation = result["attested_inventory"]["attestation"]
    assert attestation["remote_evidence_sha256"] == "f" * 64
    assert attestation["identity_mapping"][0]["remote_restore_acknowledged"] is True
    assert attestation["identity_mapping"][0]["remote_restore_operation_id"] == "close-reservation-one"
