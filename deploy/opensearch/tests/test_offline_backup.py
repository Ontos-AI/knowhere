"""A full backup is data-verified only by reading its admitted restored clone."""

from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

import httpx
import pytest


def loadVerification() -> type:
    directory: Path = Path(__file__).parents[1]
    sys.path.insert(0, str(directory))
    path: Path = directory / "offline_backup.py"
    assert path.is_file(), "Independent bounded restore/read verification is absent"
    specification = importlib.util.spec_from_file_location("offline_backup", path)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return cast(type, module.OfflineBackupVerification)


def createExport() -> dict[str, object]:
    inventory: list[dict[str, object]] = [{"name": "knowhere-one", "uuid": "old-one", "state": "closed", "primary_shards": 1}]
    boundary: dict[str, object] = {"version": 1, "captured_at_millis": 1000, "builds": [{"build_id": "build-one", "unit_count": 2, "unit_sha256": "a" * 64}]}
    return {"version": 1, "mode": "actual", "status": "exported", "control": {"cluster_uuid": "source", "epoch": 4},
        "inventory": inventory, "boundary": boundary,
        "inventory_sha256": sha256(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "boundary_sha256": sha256(json.dumps(boundary, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


class LocalAdmission:
    """Protocol double; actual shared PG admission has separate contracts."""

    def reserve(self, plan: dict[str, object], *, nativeOpenNames: tuple[str, ...], issuer: dict[str, object]) -> dict[str, object]:
        return {**plan, "status": "reserved", "cap": 400, "charged_after": len(nativeOpenNames) + len(plan["indices"]),
            "issuer_token": issuer.get("invocation_token")}

    def recordReady(self, proof: dict[str, object], *, observedIndexes: dict[str, str], issuer: dict[str, object]) -> dict[str, object]:
        return {**proof, "status": "ready", "indices": [{**row, "index_uuid": observedIndexes[row["index_name"]], "state": "open"} for row in proof["indices"]]}

    def beginClose(self, proof: dict[str, object], *, verificationReceipt: dict[str, object], issuer: dict[str, object]) -> dict[str, object]:
        assert verificationReceipt["data_verified"] is True
        return {**proof, "status": "closing", "batch_reservation_id": "33333333-3333-3333-3333-333333333333"}

    def release(self, proof: dict[str, object], *, observedClosed: dict[str, str], closeAcknowledged: bool, issuer: dict[str, object]) -> dict[str, object]:
        assert closeAcknowledged and observedClosed
        return {**proof, "status": "closed"}

    def retainUnknown(self, proof: dict[str, object], *, issuer: dict[str, object]) -> dict[str, object]:
        return {**proof, "status": "unknown"}


@pytest.mark.parametrize("shouldCleanup", [False, True])
def test_full_backup_requires_actual_payload_read_and_releases_only_acknowledged_closed_clone(shouldCleanup: bool) -> None:
    verification: type = loadVerification()
    exported: dict[str, object] = createExport()
    calls: list[str] = []
    hasRestored: bool = False
    hasClosed: bool = False
    hasDeleted: bool = False
    clone: str = ""
    state: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal hasRestored, hasClosed, hasDeleted, clone
        calls.append(request.method + " " + request.url.path)
        if request.url.path.startswith("/_cluster/state"):
            return httpx.Response(200, json={"cluster_uuid": "offline", "nodes": {"one": {"ephemeral_id": "offline-node"}}})
        if request.url.path.startswith("/_nodes/settings"):
            return httpx.Response(200, json={"nodes": {"one": {"settings": {"cluster.remote_store.state.enabled": "false"}}}})
        if request.url.path == "/_snapshot/knowhere-full":
            return httpx.Response(200, json={"knowhere-full": {"settings": {"readonly": "true", "remote_store_index_shallow_copy": "false", "shallow_snapshot_v2": "false"}}})
        if request.url.path.endswith("/_status"):
            return httpx.Response(200, json={"snapshots": [{"uuid": "snapshot-one", "state": "SUCCESS", "indices": {"knowhere-one": {"shards": {"0": {"stage": "DONE"}}}}}]})
        if request.url.path == "/_snapshot/knowhere-full/full-one":
            return httpx.Response(200, json={"snapshots": [{"uuid": "snapshot-one", "state": "SUCCESS", "indices": ["knowhere-one"],
                "remote_store_index_shallow_copy": False, "shallow_snapshot_v2": False, "metadata": {"inventory_sha256": exported["inventory_sha256"], "boundary_sha256": exported["boundary_sha256"]}}]})
        if request.url.path.endswith("/_restore"):
            assert state["pending"]["phase"] == "reserved"
            assert state["pending"]["shared_reservation"]["status"] == "reserved"
            clone = state["pending"]["indices"][0]["index_name"]
            hasRestored = True
            return httpx.Response(200, json={"accepted": True})
        if request.url.path.endswith("/_close"):
            assert state["pending"]["phase"] == "verified-closing"
            assert request.headers["X-Opaque-Id"] == "33333333-3333-3333-3333-333333333333"
            hasClosed = True
            return httpx.Response(200, json={"acknowledged": True, "shards_acknowledged": True, "indices": {clone: {"closed": True}}})
        if request.method == "DELETE":
            assert state["sealed_verification_sha256"] and request.url.path == "/" + clone
            assert hasClosed
            hasDeleted = True
            return httpx.Response(200, json={"acknowledged": True})
        if request.url.path.startswith("/_cat/"):
            return httpx.Response(200, json=[{"index": clone, "uuid": "new-one", "status": "close" if hasClosed else "open", "health": "green"}] if hasRestored and not hasDeleted else [])
        raise AssertionError(request.url.path)

    def persist(value: dict[str, object]) -> None:
        state.clear()
        state.update(json.loads(json.dumps(value)))

    def read(mapping: dict[str, str], proof: dict[str, object], names: list[str]) -> dict[str, object]:
        calls.append("actual-payload-read")
        assert names == ["knowhere-one"] and set(mapping) == {"knowhere-one"}
        assert proof["status"] == "ready" and proof["charged_after"] == 1
        assert not hasClosed
        return {"status": "verified", "data_verified": True, "current_coverage_complete": False, "missing_current_builds": ["newer-build"]}

    with httpx.Client(base_url="http://127.0.0.1:19201", transport=httpx.MockTransport(respond)) as client:
        def verify(sourceReceipt: dict[str, object], snapshot: dict[str, object]) -> dict[str, object]:
            assert snapshot["uuid"] == "snapshot-one"
            return verification.run(client, sourceReceipt, snapshotName="full-one", snapshotUuid="snapshot-one", targetClusterUuid="offline",
                targetIncarnation="offline-node", operationId="11111111-1111-1111-1111-111111111111", batchLimit=2,
                timeoutSeconds=10, state={}, verifyPayload=read, persist=persist, admission=LocalAdmission(), readIssuer=lambda: {"invocation_token": "local-issuer"}, cleanupClones=shouldCleanup)

        if shouldCleanup:
            from backup_workflow import BackupWorkflow

            class Source:
                def export(self, **_values: object) -> dict[str, object]:
                    calls.append("authoritative-export")
                    return exported

            def sourceNative(request: httpx.Request) -> httpx.Response:
                path: str = request.url.path
                if path.startswith("/_cluster/state/metadata"):
                    return httpx.Response(200, json={"metadata": {"indices": {"knowhere-one": {"state": "close",
                        "settings": {"index": {"uuid": "old-one", "number_of_shards": "1"}}}}}})
                if path.startswith("/_cluster/state/nodes"):
                    return httpx.Response(200, json={"cluster_uuid": "source", "nodes": {"one": {"ephemeral_id": "source-node"}}})
                if request.method == "PUT":
                    calls.append("snapshot-submitted")
                    return httpx.Response(200, json={"accepted": True})
                if path.endswith("/_status"):
                    return httpx.Response(200, json={"snapshots": [{"snapshot": "full-one", "uuid": "snapshot-one", "state": "SUCCESS",
                        "indices": {"knowhere-one": {"shards": {"0": {"stage": "DONE"}}}}}]})
                return httpx.Response(200, json={"snapshots": [{"snapshot": "full-one", "uuid": "snapshot-one", "state": "SUCCESS",
                    "indices": ["knowhere-one"], "remote_store_index_shallow_copy": False, "shallow_snapshot_v2": False,
                    "metadata": {"inventory_sha256": exported["inventory_sha256"], "boundary_sha256": exported["boundary_sha256"]},
                    "shards": {"total": 1, "successful": 1, "failed": 0}, "start_time_in_millis": 2_000, "end_time_in_millis": 3_000}]})

            with httpx.Client(base_url="http://127.0.0.1:19200", transport=httpx.MockTransport(sourceNative)) as sourceClient:
                point: dict[str, object] = BackupWorkflow.run(Source(), sourceClient, indexPrefix="knowhere", snapshotName="full-one",
                    expectedEpoch=4, expectedClusterUuid="source", expectedIncarnation="source-node", state={}, timeoutSeconds=10,
                    verifyIndependent=verify, persist=lambda _value: None)
                assert point["status"] == "verified" and point["coverage_verified"] is True
                nextCycle: dict[str, object] = BackupWorkflow.run(Source(), sourceClient, indexPrefix="knowhere", snapshotName="too-soon",
                    expectedEpoch=4, expectedClusterUuid="source", expectedIncarnation="source-node",
                    state=cast(dict[str, object], point["state"]), timeoutSeconds=10,
                    verifyIndependent=verify, persist=lambda _value: pytest.fail("Not due means no snapshot mutation"))
                assert nextCycle["status"] == "not-due"
            assert calls[:2] == ["authoritative-export", "snapshot-submitted"]
            result = cast(dict[str, object], point["point"])
            result["serving"] = False
        else:
            result = verify(exported, {"uuid": "snapshot-one"})
        resumed = verification.run(client, exported, snapshotName="full-one", snapshotUuid="snapshot-one", targetClusterUuid="offline",
            targetIncarnation="offline-node", operationId="11111111-1111-1111-1111-111111111111", batchLimit=2,
            timeoutSeconds=10, state=dict(state), verifyPayload=lambda *_arguments: pytest.fail("Known sealed batch was replayed"),
            persist=persist, admission=LocalAdmission(), readIssuer=lambda: {"invocation_token": "new-local-issuer"}, cleanupClones=shouldCleanup)
    assert result["data_verified"] is True
    assert result["current_coverage_complete"] is False
    assert result["serving"] is False
    assert state["pending"] is None
    assert resumed["data_verified"] is True
    assert hasDeleted is shouldCleanup
    assert sum(call.endswith("/_restore") for call in calls) == 1
    assert calls.index("actual-payload-read") < next(index for index, call in enumerate(calls) if call.endswith("/_close"))


def test_unknown_restore_keeps_charge_and_never_reads_or_sends_opposite_close() -> None:
    verification: type = loadVerification()
    state: dict[str, object] = {"pending": {"phase": "uncertain", "indices": [{"index_name": "verify-old", "reservation_id": "old"}]}}
    with httpx.Client(base_url="http://127.0.0.1:19201", transport=httpx.MockTransport(lambda _request: pytest.fail("Unknown issuer cannot issue another native request"))) as client:
        with pytest.raises(ValueError, match="reconciliation"):
            verification.run(client, createExport(), snapshotName="full-one", snapshotUuid="snapshot-one", targetClusterUuid="offline",
                targetIncarnation="offline-node", operationId="11111111-1111-1111-1111-111111111111", batchLimit=2,
                timeoutSeconds=10, state=state, verifyPayload=lambda *_values: pytest.fail("No read"),
                admission=LocalAdmission(), readIssuer=lambda: {})
