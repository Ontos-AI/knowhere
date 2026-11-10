"""Bounded full restore/read verification on a distinct non-serving target.

The source OPEN/CLOSED catalog is never opened. A renamed snapshot batch consumes
durable offline-target capacity before restore. Only an actual payload verifier
and acknowledged exact-UUID closure release that batch. Unknown outcomes remain
charged and prohibit automatic retry/opposite operations. This target never adopts
application control or rewinds current published selections.
"""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
import json
import re
import time
from typing import Protocol, cast
from uuid import UUID, uuid5

import httpx

__all__: list[str] = ["OfflineBackupVerification"]


class _Admission(Protocol):
    def reserve(self, plan: dict[str, object], *, nativeOpenNames: tuple[str, ...], issuer: dict[str, object]) -> dict[str, object]: ...
    def recordReady(self, proof: dict[str, object], *, observedIndexes: dict[str, str], issuer: dict[str, object]) -> dict[str, object]: ...
    def beginClose(self, proof: dict[str, object], *, verificationReceipt: dict[str, object], issuer: dict[str, object]) -> dict[str, object]: ...
    def release(self, proof: dict[str, object], *, observedClosed: dict[str, str], closeAcknowledged: bool, issuer: dict[str, object]) -> dict[str, object]: ...
    def retainUnknown(self, proof: dict[str, object], *, issuer: dict[str, object]) -> dict[str, object]: ...


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Offline verification expects an object")
    return cast(dict[str, object], value)


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _request(client: httpx.Client, method: str, path: str, body: object | None = None,
        reservationId: str | None = None) -> object:
    response: httpx.Response = client.request(method, path, json=body,
        headers={"X-Opaque-Id": reservationId} if reservationId else {})
    response.raise_for_status()
    return response.json()


def _catalog(client: httpx.Client) -> list[dict[str, object]]:
    value: object = _request(client, "GET", "/_cat/indices?format=json&h=index,uuid,status,health&expand_wildcards=all")
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("Offline target catalog is invalid")
    rows: list[dict[str, object]] = cast(list[dict[str, object]], value)
    if len({row.get("index") for row in rows}) != len(rows):
        raise ValueError("Offline target catalog contains duplicate identities")
    return rows


def _groupPaths(names: list[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    size: int = 0
    for name in names:
        if not groups or size + len(name) + 1 > 2000:
            groups.append([])
            size = 0
        groups[-1].append(name)
        size += len(name) + 1
    return groups


class OfflineBackupVerification:
    @staticmethod
    def _assertTarget(client: httpx.Client, exported: dict[str, object], clusterUuid: str, incarnation: str) -> None:
        state: dict[str, object] = _object(_request(client, "GET", "/_cluster/state/nodes?filter_path=cluster_uuid,nodes.*.ephemeral_id"))
        nodes: dict[str, object] = _object(state.get("nodes"))
        source: dict[str, object] = _object(exported.get("control"))
        if (state.get("cluster_uuid") != clusterUuid or clusterUuid == source.get("cluster_uuid")
                or len(nodes) != 1 or _object(next(iter(nodes.values()))).get("ephemeral_id") != incarnation):
            raise ValueError("Verification target must be the exact distinct approved offline cluster/incarnation")

    @staticmethod
    def run(client: httpx.Client, exportedReceipt: dict[str, object], *, snapshotName: str, snapshotUuid: str,
            targetClusterUuid: str, targetIncarnation: str, operationId: str, batchLimit: int, timeoutSeconds: float,
            state: dict[str, object], verifyPayload: Callable[[dict[str, str], dict[str, object], list[str]], dict[str, object]],
            admission: _Admission, readIssuer: Callable[[], dict[str, object]], cleanupClones: bool = False,
            persist: Callable[[dict[str, object]], None] | None = None, now: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep) -> dict[str, object]:
        token: UUID = UUID(operationId)
        if state.get("pending") is not None:
            raise ValueError("Offline restore issuer requires physical/native reconciliation before any new request")
        if persist is None:
            raise ValueError("Offline restore requires a durable write-ahead state callback")
        if not 1 <= batchLimit <= 400 or timeoutSeconds <= 0 or re.fullmatch(r"[a-z0-9][a-z0-9_.-]+", snapshotName) is None:
            raise ValueError("Offline verification requires bounded capacity/time and exact snapshot name")
        inventory: list[dict[str, object]] = cast(list[dict[str, object]], exportedReceipt.get("inventory", []))
        names: list[str] = [str(index["name"]) for index in inventory]
        if (not names or len(set(names)) != len(names) or any(re.fullmatch(r"[a-z0-9][a-z0-9_.-]+", name) is None for name in names)
                or exportedReceipt.get("inventory_sha256") != _digest(inventory)
                or exportedReceipt.get("boundary_sha256") != _digest(exportedReceipt.get("boundary"))):
            raise ValueError("Exported authoritative inventory/boundary identity is invalid")
        OfflineBackupVerification._assertTarget(client, exportedReceipt, targetClusterUuid, targetIncarnation)
        settings: dict[str, object] = _object(_request(client, "GET", "/_nodes/settings?flat_settings=true"))
        for node in _object(settings.get("nodes")).values():
            configured: dict[str, object] = _object(_object(node).get("settings"))
            if configured.get("cluster.remote_store.state.enabled") not in (False, "false") or any(str(key).startswith("node.attr.remote_store.") for key in configured):
                raise ValueError("Independent verification target must disable live remote stores/state")
        repository: dict[str, object] = _object(_object(_request(client, "GET", "/_snapshot/knowhere-full"))["knowhere-full"])
        repositorySettings: dict[str, object] = _object(repository["settings"])
        if (repositorySettings.get("readonly") not in (True, "true")
                or repositorySettings.get("remote_store_index_shallow_copy", False) not in (False, "false")
                or repositorySettings.get("shallow_snapshot_v2", False) not in (False, "false")):
            raise ValueError("Offline verification requires the independent full repository mounted readonly")
        snapshots: list[dict[str, object]] = cast(list[dict[str, object]], _object(_request(client, "GET", f"/_snapshot/knowhere-full/{snapshotName}"))["snapshots"])
        snapshot: dict[str, object] = snapshots[0] if len(snapshots) == 1 else {}
        if (snapshot.get("uuid") != snapshotUuid or snapshot.get("state") != "SUCCESS"
                or set(cast(list[str], snapshot.get("indices", []))) != set(names)
                or snapshot.get("remote_store_index_shallow_copy") is not False or snapshot.get("shallow_snapshot_v2", False) is not False
                or _object(snapshot.get("metadata")).get("inventory_sha256") != exportedReceipt["inventory_sha256"]
                or _object(snapshot.get("metadata")).get("boundary_sha256") != exportedReceipt["boundary_sha256"]):
            raise ValueError("Offline repository does not identify the exact full captured boundary/catalog")
        statuses: list[dict[str, object]] = cast(list[dict[str, object]], _object(_request(client, "GET", f"/_snapshot/knowhere-full/{snapshotName}/_status"))["snapshots"])
        shardStatus: dict[str, object] = statuses[0] if len(statuses) == 1 else {}
        shardIndices: dict[str, object] = _object(shardStatus.get("indices"))
        if shardStatus.get("uuid") != snapshotUuid or shardStatus.get("state") != "SUCCESS" or set(shardIndices) != set(names):
            raise ValueError("Snapshot does not have complete exact shard/catalog status")
        for index in inventory:
            shards: dict[str, object] = _object(_object(shardIndices[str(index["name"])])["shards"])
            if set(shards) != {str(number) for number in range(int(str(index["primary_shards"])))} or any(_object(shard).get("stage") != "DONE" for shard in shards.values()):
                raise ValueError("An expected OPEN/CLOSED primary is not fully snapshotted")
        progress: dict[str, object] = {"version": 1, "operation_id": operationId, "snapshot_uuid": snapshotUuid,
            "target_cluster_uuid": targetClusterUuid, "target_incarnation": targetIncarnation,
            "inventory_sha256": exportedReceipt["inventory_sha256"], "boundary_sha256": exportedReceipt["boundary_sha256"],
            "pending": None, "verified_batches": list(cast(list[dict[str, object]], state.get("verified_batches", [])))}
        if state and any(state.get(key) != progress[key] for key in ("operation_id", "snapshot_uuid", "target_cluster_uuid", "target_incarnation", "inventory_sha256", "boundary_sha256")):
            raise ValueError("Offline progress belongs to another source, snapshot, operation or target identity")
        results: list[dict[str, object]] = []
        prefix: str = "verify-" + token.hex + "-"
        deadline: float = now() + timeoutSeconds
        completedClones: dict[str, str] = {}
        completedOriginals: set[str] = set()
        removed: set[str] = set(cast(list[str], state.get("cleanup_deleted", [])))
        liveBefore: dict[str, dict[str, object]] = {str(row["index"]): row for row in _catalog(client)}
        for batch in cast(list[dict[str, object]], progress["verified_batches"]):
            payload: dict[str, object] = _object(batch.get("payload_receipt"))
            if batch.get("payload_receipt_sha256") != _digest(payload) or payload.get("data_verified") is not True:
                raise ValueError("A prior completed batch lacks its sealed payload receipt")
            for entry in cast(list[dict[str, object]], batch["indices"]):
                original: str = str(entry["original_index_name"])
                name: str = str(entry["index_name"])
                identity: str = str(entry["index_uuid"])
                if (original not in names or original in completedOriginals or name != prefix + original
                        or name not in removed and (name not in liveBefore or liveBefore[name].get("status") != "close" or liveBefore[name].get("uuid") != identity)):
                    raise ValueError("Known completed clone UUID/state changed; do not replay it")
                completedOriginals.add(original)
                completedClones[name] = identity
            results.append(payload)

        def save() -> None:
            if persist is not None:
                persist(progress)

        outstanding: list[str] = [name for name in names if name not in completedOriginals]
        for offset in range(0, len(outstanding), batchLimit):
            selected: list[str] = outstanding[offset:offset + batchLimit]
            mapping: dict[str, str] = {name: prefix + name for name in selected}
            observed: list[dict[str, object]] = _catalog(client)
            if any(row.get("index") in mapping.values() for row in observed):
                raise ValueError("Verification clone identity already exists; reconcile instead of replacing it")
            occupied: set[str] = {str(row["index"]) for row in observed if not str(row["index"]).startswith(".") and row.get("status") == "open"}
            if len(occupied) + len(mapping) > 400:
                raise ValueError("Offline restore cannot exceed 400 charged native/reserved/uncertain business slots")
            reservation: str = str(uuid5(token, ":".join(selected)))
            entries: list[dict[str, object]] = [{"original_index_name": original, "index_name": clone,
                "reservation_id": reservation, "state": "opening"} for original, clone in mapping.items()]
            pending: dict[str, object] = {"phase": "reserved", "reservation_id": reservation, "indices": entries,
                "charged_after": len(occupied) + len(mapping)}
            plan: dict[str, object] = {"version": 1, "purpose": "offline-backup-verification",
                "source_cluster_uuid": _object(exportedReceipt["control"])["cluster_uuid"],
                "target_cluster_uuid": targetClusterUuid, "target_incarnation": targetIncarnation, "snapshot_uuid": snapshotUuid,
                "inventory_sha256": exportedReceipt["inventory_sha256"], "boundary_sha256": exportedReceipt["boundary_sha256"],
                "operation_id": operationId, "batch_reservation_id": reservation, "indices": entries}
            progress["pending"] = pending
            save()
            proof: dict[str, object] = admission.reserve(plan, nativeOpenNames=tuple(sorted(occupied)), issuer=readIssuer())
            if proof.get("status") != "reserved" or not isinstance(proof.get("charged_after"), int) or int(str(proof["charged_after"])) > 400:
                raise ValueError("Shared offline authority did not reserve the exact bounded batch")
            pending.update({"shared_reservation": proof, "charged_after": proof["charged_after"]})
            save()
            try:
                OfflineBackupVerification._assertTarget(client, exportedReceipt, targetClusterUuid, targetIncarnation)
                accepted: dict[str, object] = _object(_request(client, "POST", f"/_snapshot/knowhere-full/{snapshotName}/_restore?wait_for_completion=false", {
                    "indices": selected, "rename_pattern": "^(.+)$", "rename_replacement": prefix + "$1",
                    "include_global_state": False, "include_aliases": False, "partial": False, "storage_type": "local",
                }, reservation))
                if accepted.get("accepted") is not True:
                    raise ValueError("Independent restore was not acknowledged")
                pending["phase"] = "restoring"
                save()
                while now() < deadline:
                    rows: dict[str, dict[str, object]] = {str(row["index"]): row for row in _catalog(client)}
                    if all(name in rows and rows[name].get("status") == "open" and rows[name].get("health") in ("green", "yellow") and rows[name].get("uuid") for name in mapping.values()):
                        break
                    sleep(min(0.1, max(0, deadline - now())))
                else:
                    raise TimeoutError("Independent restore readiness remains uncertain")
                for entry in entries:
                    entry.update({"index_uuid": rows[str(entry["index_name"])]["uuid"], "state": "open"})
                proof = admission.recordReady(proof, observedIndexes={str(entry["index_name"]): str(entry["index_uuid"]) for entry in entries}, issuer=readIssuer())
                if proof.get("status") != "ready":
                    raise ValueError("Shared offline authority did not record exact READY identities")
                checked: dict[str, object] = verifyPayload(mapping, proof, selected)
                if checked.get("status") != "verified" or checked.get("data_verified") is not True:
                    raise ValueError("Restored native payload did not verify the captured immutable build boundary")
                results.append(checked)
                proof = admission.beginClose(proof, verificationReceipt=checked, issuer=readIssuer())
                if proof.get("status") != "closing" or proof.get("batch_reservation_id") == reservation:
                    raise ValueError("Shared offline authority did not install a distinct verified close fence")
                pending["phase"] = "verified-closing"
                pending["shared_reservation"] = proof
                save()
                for group in _groupPaths(list(mapping.values())):
                    OfflineBackupVerification._assertTarget(client, exportedReceipt, targetClusterUuid, targetIncarnation)
                    close: dict[str, object] = _object(_request(client, "POST", "/" + ",".join(group) + "/_close?timeout=10s&wait_for_active_shards=1", reservationId=str(proof["batch_reservation_id"])))
                    if (close.get("acknowledged") is not True or close.get("shards_acknowledged") is not True
                            or close.get("indices") != {name: {"closed": True} for name in group}):
                        raise ValueError("Verification clone closure is not acknowledged")
                closed: dict[str, dict[str, object]] = {str(row["index"]): row for row in _catalog(client)}
                if any(name not in closed or closed[name].get("status") != "close" or closed[name].get("uuid") != entry["index_uuid"] for entry in entries for name in (str(entry["index_name"]),)):
                    raise ValueError("Verification clone UUID/CLOSED reconciliation failed")
                identities: dict[str, str] = {str(entry["index_name"]): str(entry["index_uuid"]) for entry in entries}
                released: dict[str, object] = admission.release(proof, observedClosed=identities, closeAcknowledged=True, issuer=readIssuer())
                if released.get("status") != "closed":
                    raise ValueError("Shared offline close charge remains unresolved")
                completedClones.update(identities)
                cast(list[dict[str, object]], progress["verified_batches"]).append({"indices": entries, "data_verified": True,
                    "payload_receipt": checked, "payload_receipt_sha256": _digest(checked), "close_receipt": released})
                progress["pending"] = None
                save()
            except (httpx.HTTPError, ValueError, TimeoutError):
                pending["phase"] = "uncertain"
                save()
                admission.retainUnknown(proof, issuer=readIssuer())
                raise
        if cleanupClones:
            progress["sealed_verification_sha256"] = _digest(results)
            progress["cleanup_deleted"] = sorted(removed)
            save()
            for group in _groupPaths([name for name in completedClones if name not in removed]):
                OfflineBackupVerification._assertTarget(client, exportedReceipt, targetClusterUuid, targetIncarnation)
                catalog: dict[str, dict[str, object]] = {str(row["index"]): row for row in _catalog(client)}
                if any(not name.startswith(prefix) or name not in catalog or catalog[name].get("uuid") != completedClones[name] or catalog[name].get("status") != "close" for name in group):
                    raise ValueError("Owned verification clone cleanup lost its exact sealed CLOSED identity")
                progress["pending"] = {"phase": "verified-clone-cleanup-intent", "indices": group}
                save()
                deleted: dict[str, object] = _object(_request(client, "DELETE", "/" + ",".join(group), reservationId=str(uuid5(token, "delete:" + ":".join(group)))))
                if deleted.get("acknowledged") is not True or any(row["index"] in group for row in _catalog(client)):
                    raise ValueError("Owned verification clone cleanup remains uncertain; do not replay it")
                removed.update(group)
                progress.update({"pending": None, "cleanup_deleted": sorted(removed)})
                save()
        return {"version": 1, "mode": "actual", "status": "verified", "data_verified": True, "catalog_verified": True,
            "boundary_sha256": exportedReceipt["boundary_sha256"], "inventory_sha256": exportedReceipt["inventory_sha256"],
            "snapshot_uuid": snapshotUuid, "target_cluster_uuid": targetClusterUuid, "target_incarnation": targetIncarnation,
            "current_coverage_complete": all(result.get("current_coverage_complete") is True for result in results),
            "batches": results, "serving": False, "cleanup_deleted": sorted(removed), "state": progress}
