"""Authoritative export, full snapshot and independent payload verification.

This coordinator runs on the existing operations host. Both native clients are
constructed by the approved CLI, and the independent verifier restores only on a
distinct admitted target. Catalog success and data verification stay separate.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
import json
import time
from typing import Protocol, cast

import httpx

from operations import OpenSearchOperations

__all__: list[str] = ["BackupWorkflow"]


class _Source(Protocol):
    def export(self, **values: object) -> dict[str, object]: ...


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Backup workflow expects an object")
    return cast(dict[str, object], value)


def _request(client: httpx.Client, method: str, path: str, body: object | None = None) -> object:
    response: httpx.Response = client.request(method, path, json=body)
    response.raise_for_status()
    return response.json()


def _catalog(client: httpx.Client, prefix: str) -> list[dict[str, object]]:
    state: dict[str, object] = _object(_request(client, "GET", f"/_cluster/state/metadata/{prefix}-*"
        "?ignore_unavailable=false&expand_wildcards=all&filter_path=metadata.indices.*.state,"
        "metadata.indices.*.settings.index.uuid,metadata.indices.*.settings.index.number_of_shards"))
    rows: dict[str, object] = _object(_object(state.get("metadata")).get("indices"))
    catalog: list[dict[str, object]] = []
    manifestIndex: str = f"{prefix}-published-manifests"
    for name, metadata in rows.items():
        if name == manifestIndex:
            # Published manifests are rebuilt from SQL on restore.
            continue
        row: dict[str, object] = _object(metadata)
        settings: dict[str, object] = _object(_object(row.get("settings")).get("index"))
        if not name.startswith(prefix + "-") or row.get("state") not in ("open", "close"):
            raise ValueError("Source cluster-state catalog does not identify a managed OPEN/CLOSED index")
        catalog.append({"name": name, "uuid": settings["uuid"], "state": "closed" if row["state"] == "close" else "open",
            "primary_shards": int(str(settings["number_of_shards"]))})
    return catalog


def _assertIdentity(client: httpx.Client, cluster: str, incarnation: str) -> None:
    state: dict[str, object] = _object(_request(client, "GET", "/_cluster/state/nodes?filter_path=cluster_uuid,nodes.*.ephemeral_id"))
    nodes: dict[str, object] = _object(state.get("nodes"))
    if state.get("cluster_uuid") != cluster or len(nodes) != 1 or _object(next(iter(nodes.values()))).get("ephemeral_id") != incarnation:
        raise ValueError("Approved backup endpoint differs from the actual source native identity")


class BackupWorkflow:
    @staticmethod
    def run(source: _Source, client: httpx.Client, *, indexPrefix: str, snapshotName: str,
            expectedEpoch: int, expectedClusterUuid: str, expectedIncarnation: str, state: dict[str, object],
            timeoutSeconds: float, verifyIndependent: Callable[[dict[str, object], dict[str, object]], dict[str, object]],
            persist: Callable[[dict[str, object]], None], now: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep) -> dict[str, object]:
        if timeoutSeconds <= 0:
            raise ValueError("Full backup workflow requires a bounded operation duration")
        pendingValue: object = state.get("pending")
        pending: dict[str, object] | None = _object(pendingValue) if pendingValue is not None else None
        points: list[dict[str, object]] = list(cast(list[dict[str, object]], state.get("points", [])))
        lastSubmission: int = int(str(state.get("last_submission_at_millis", 0)))
        if pending is None and lastSubmission and int(datetime.now(timezone.utc).timestamp() * 1000) < lastSubmission + 3_600_000:
            return {"mode": "actual", "status": "not-due", "data_verified": False, "coverage_verified": False,
                "next_due_at_millis": lastSubmission + 3_600_000, "state": state}
        if pending is None:
            exported: dict[str, object] = source.export(expectedEpoch=expectedEpoch,
                expectedClusterUuid=expectedClusterUuid, expectedIncarnation=expectedIncarnation)
            if exported.get("status") != "exported" or exported.get("mode") != "actual" or exported.get("errors", []) != []:
                raise ValueError("Source-owned authoritative export did not complete")
            _assertIdentity(client, expectedClusterUuid, expectedIncarnation)
            inventory: object = exported["inventory"]
            if sorted(_catalog(client, indexPrefix), key=lambda row: str(row["name"])) != sorted(cast(list[dict[str, object]], inventory), key=lambda row: str(row["name"])):
                raise ValueError("Authoritative source catalog differs from actual OPEN/CLOSED native inventory")
            plan: dict[str, object] = OpenSearchOperations.planSnapshot(inventory,
                boundarySha256=str(exported["boundary_sha256"]), snapshotName=snapshotName)
            pending = {"phase": "snapshot-intent", "snapshot": snapshotName, "exported": exported,
                "intent_at_millis": int(datetime.now(timezone.utc).timestamp() * 1000)}
            persist({"pending": pending, "points": points})
            request: dict[str, object] = _object(plan["request"])
            try:
                response: dict[str, object] = _object(_request(client, str(request["method"]), str(request["path"]), request["body"]))
                if response.get("accepted") is not True:
                    raise ValueError("Full snapshot submission was not acknowledged")
            except (httpx.HTTPError, ValueError):
                pending["phase"] = "snapshot-unknown"
                persist({"pending": pending, "points": points})
                raise
            pending["phase"] = "snapshot-submitted"
            persist({"pending": pending, "points": points})
        else:
            if pending.get("phase") not in ("snapshot-submitted", "awaiting-independent-verification"):
                raise ValueError("Unknown snapshot issuer must be reconciled without another submission")
            if pending.get("snapshot") != snapshotName:
                raise ValueError("Backup resume must retain the exact admitted snapshot identity")
            exported = _object(pending["exported"])
            if _object(exported["control"]).get("cluster_uuid") != expectedClusterUuid:
                raise ValueError("Pending snapshot source differs from the current approved cluster")
            _assertIdentity(client, expectedClusterUuid, expectedIncarnation)
        deadline: float = now() + timeoutSeconds
        while now() < deadline:
            observed: dict[str, object] = _object(_request(client, "GET", f"/_snapshot/knowhere-full/{snapshotName}"))
            snapshots: list[dict[str, object]] = cast(list[dict[str, object]], observed.get("snapshots", []))
            if len(snapshots) != 1 or snapshots[0].get("snapshot") != snapshotName:
                raise ValueError("Snapshot poll does not identify its admitted operation")
            snapshot: dict[str, object] = snapshots[0]
            if snapshot.get("state") != "IN_PROGRESS":
                break
            sleep(min(1, max(0, deadline - now())))
        else:
            return {"mode": "actual", "status": "snapshot-in-progress", "data_verified": False, "coverage_verified": False,
                "state": {"pending": pending, "points": points}}
        if snapshot.get("state") in ("FAILED", "PARTIAL"):
            failure: dict[str, object] = {"snapshot": snapshotName, "snapshot_uuid": snapshot.get("uuid"), "state": snapshot.get("state"),
                "coverage_verified": False, "data_verified": False, "completed_at_millis": snapshot.get("end_time_in_millis", 0)}
            state = {"pending": None, "points": [*points, failure], "last_submission_at_millis": pending["intent_at_millis"]}
            persist(state)
            return {"mode": "actual", "status": "snapshot-failed", "data_verified": False, "coverage_verified": False, "point": failure, "state": state}
        if snapshot.get("state") != "SUCCESS":
            raise ValueError("Native snapshot did not report a known terminal state")
        status: dict[str, object] = _object(_request(client, "GET", f"/_snapshot/knowhere-full/{snapshotName}/_status"))
        pending["phase"] = "awaiting-independent-verification"
        persist({"pending": pending, "points": points})
        # Evaluate structural integrity alone before reserving or restoring
        # anything on the independent target. This is never a data-proof result.
        structural: dict[str, object] = OpenSearchOperations.verifySnapshot(exported["inventory"], snapshot,
            observedInventory=_catalog(client, indexPrefix), boundarySha256=str(exported["boundary_sha256"]),
            verifiedBoundarySha256=None, shardStatus=status, verifiedAt=int(datetime.now(timezone.utc).timestamp() * 1000),
            boundaryCapturedAtMillis=int(str(_object(exported["boundary"])["captured_at_millis"])))
        if structural["errors"] != ["Document/revision/build/update/delete boundary is not independently verified"]:
            raise ValueError("Snapshot catalog, identity, timing or shard evidence differs from the source-owned capture")
        independent: dict[str, object] = verifyIndependent(exported, snapshot)
        isVerified: bool = (independent.get("status") == "verified" and independent.get("data_verified") is True
            and independent.get("snapshot_uuid") == snapshot.get("uuid")
            and independent.get("inventory_sha256") == exported["inventory_sha256"]
            and independent.get("boundary_sha256") == exported["boundary_sha256"])
        if not isVerified:
            return {"mode": "actual", "status": "awaiting-independent-verification", "data_verified": False,
                "coverage_verified": False, "catalog_snapshot_state": snapshot.get("state"),
                "independent_verification": independent, "state": {"pending": pending, "points": points}}
        point: dict[str, object] = OpenSearchOperations.verifySnapshot(exported["inventory"], snapshot,
            observedInventory=_catalog(client, indexPrefix), boundarySha256=str(exported["boundary_sha256"]),
            verifiedBoundarySha256=str(independent["boundary_sha256"]), shardStatus=status,
            verifiedAt=int(datetime.now(timezone.utc).timestamp() * 1000),
            boundaryCapturedAtMillis=int(str(_object(exported["boundary"])["captured_at_millis"])))
        point.update({"data_verified": point["coverage_verified"] is True,
            "independent_verification_sha256": sha256(json.dumps(independent, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "current_coverage_complete": independent.get("current_coverage_complete") is True,
            "missing_current_builds": independent.get("missing_current_builds", [])})
        if point["data_verified"] is True:
            points.append(point)
            state = {"pending": None, "points": points, "last_submission_at_millis": pending["intent_at_millis"]}
        else:
            state = {"pending": pending, "points": points}
        persist(state)
        return {"mode": "actual", "status": "verified" if point["data_verified"] else "awaiting-independent-verification",
            "data_verified": point["data_verified"], "coverage_verified": point["coverage_verified"], "point": point,
            "state": state, "service_restored": False}
