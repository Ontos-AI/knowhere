"""Hourly backup progression and native-only retention on an off-node scheduler.

The caller supplies a checksummed complete catalog and published boundary from
the approved application exporter. A boundary verification receipt remains
mandatory before a completed snapshot counts toward the three-point floor.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import httpx

from operations import OpenSearchOperations


def _request(client: httpx.Client, method: str, path: str, body: object | None = None) -> dict[str, object]:
    response: httpx.Response = client.request(method, path, json=body)
    response.raise_for_status()
    decoded: object = response.json()
    if not isinstance(decoded, dict):
        raise ValueError("Native backup response must be an object")
    return cast(dict[str, object], decoded)


def _inventory(client: httpx.Client, indexPrefix: str) -> list[dict[str, object]]:
    response: httpx.Response = client.get(f"/_cat/indices/{indexPrefix}-*?format=json&h=index,uuid,status,pri&expand_wildcards=all")
    response.raise_for_status()
    rows: list[dict[str, str]] = response.json()
    # Published manifests are rebuilt from SQL on restore.
    manifestIndex: str = f"{indexPrefix}-published-manifests"
    return [{"name": row["index"], "uuid": row["uuid"], "state": "closed" if row["status"] == "close" else "open", "primary_shards": int(row["pri"])}
        for row in rows if row["index"] != manifestIndex]


class BackupOperations:
    @staticmethod
    def runCycle(client: httpx.Client, *, indexPrefix: str, inventory: object, boundarySha256: str,
            state: dict[str, object], nowMillis: int, verifiedBoundarySha256: str | None = None,
            persistState: Callable[[dict[str, object]], None] | None = None,
            boundaryCapturedAtMillis: int | None = None) -> dict[str, object]:
        points: list[dict[str, object]] = [dict(point) for point in cast(list[dict[str, object]], state.get("points", []))]
        pending: dict[str, object] | None = cast(dict[str, object] | None, state.get("pending"))
        lastSubmission: int = int(str(state.get("last_submission_at_millis", pending.get("intent_at_millis", 0) if pending else 0)))
        currentState: dict[str, object] = {"pending": pending, "points": points, "last_submission_at_millis": lastSubmission}
        if pending is None:
            if lastSubmission and nowMillis < lastSubmission + 3_600_000:
                return {"state": currentState, "not_due": True, "next_due_at_millis": lastSubmission + 3_600_000,
                    "submitted": False, "coverage_verified": False}
            plan: dict[str, object] = OpenSearchOperations.planSnapshot(inventory, boundarySha256=boundarySha256, snapshotName=f"hourly-{nowMillis}")
            current: list[dict[str, object]] = _inventory(client, indexPrefix)
            if sorted(current, key=lambda row: str(row["name"])) != sorted(cast(list[dict[str, object]], inventory), key=lambda row: str(row["name"])):
                raise ValueError("Expected application catalog differs from open/closed native inventory")
            pending = {"snapshot": f"hourly-{nowMillis}", "inventory": inventory, "boundary_sha256": boundarySha256,
                "boundary_captured_at_millis": boundaryCapturedAtMillis,
                "intent_at_millis": nowMillis, "request": plan["request"], "submission_status": "intent"}
            intent: dict[str, object] = {"pending": pending, "points": points, "last_submission_at_millis": nowMillis}
            if persistState is not None:
                persistState(intent)
            request: dict[str, object] = cast(dict[str, object], plan["request"])
            result: dict[str, object] = _request(client, str(request["method"]), str(request["path"]), request["body"])
            if result.get("accepted") is not True:
                raise ValueError("Native snapshot submission was not acknowledged")
            pending = {**pending, "submission_status": "accepted"}
            return {"state": {**intent, "pending": pending}, "submitted": True, "coverage_verified": False}
        name: str = str(pending["snapshot"])
        if not name or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for character in name):
            raise ValueError("Pending snapshot identity is invalid")
        response: dict[str, object] = _request(client, "GET", f"/_snapshot/knowhere-full/{name}")
        snapshots: list[dict[str, object]] = cast(list[dict[str, object]], response.get("snapshots", []))
        if len(snapshots) != 1 or snapshots[0].get("snapshot") != name:
            raise ValueError("Native snapshot poll did not identify the pending operation")
        snapshot: dict[str, object] = snapshots[0]
        if snapshot.get("state") == "IN_PROGRESS":
            return {"state": currentState, "waiting_for_completion": True, "coverage_verified": False}
        status: dict[str, object] = _request(client, "GET", f"/_snapshot/knowhere-full/{name}/_status")
        point: dict[str, object] = OpenSearchOperations.verifySnapshot(pending["inventory"], snapshot,
            observedInventory=_inventory(client, indexPrefix), boundarySha256=str(pending["boundary_sha256"]),
            verifiedBoundarySha256=verifiedBoundarySha256, shardStatus=status, verifiedAt=nowMillis,
            boundaryCapturedAtMillis=cast(int | None, pending.get("boundary_captured_at_millis")))
        points.append(point)
        completedState: dict[str, object] = {"pending": None, "points": points, "last_submission_at_millis": lastSubmission}
        if persistState is not None:
            persistState(completedState)
        if lastSubmission and nowMillis >= lastSubmission + 3_600_000:
            nextResult: dict[str, object] = BackupOperations.runCycle(client, indexPrefix=indexPrefix, inventory=inventory,
                boundarySha256=boundarySha256, state=completedState, nowMillis=nowMillis,
                verifiedBoundarySha256=verifiedBoundarySha256, persistState=persistState,
                boundaryCapturedAtMillis=boundaryCapturedAtMillis)
            return {**nextResult, "completed": True, "point": point}
        return {"state": completedState, "completed": True,
            "coverage_verified": point["coverage_verified"], "point": point,
            "missing_boundary_verification": verifiedBoundarySha256 is None}

    @staticmethod
    def applyRetention(client: httpx.Client, points: list[dict[str, object]], *, nowMillis: int) -> dict[str, object]:
        plan: dict[str, object] = OpenSearchOperations.planRetention(list(points), nowMillis=nowMillis)
        protected: list[str] = cast(list[str], plan["protected"])
        deleted: list[str] = []
        if not plan["retention_floor_satisfied"]:
            return {**plan, "deleted": deleted, "blocked_below_three_verified_points": True}
        byName: dict[str, dict[str, object]] = {str(point["snapshot"]): point for point in points}
        for name in cast(list[str], plan["delete"]):
            response: dict[str, object] = _request(client, "GET", "/_snapshot/knowhere-full/_all")
            current: dict[str, dict[str, object]] = {str(point["snapshot"]): point for point in cast(list[dict[str, object]], response.get("snapshots", []))}
            if any(identity not in current or current[identity].get("state") != "SUCCESS"
                    or not byName[identity].get("snapshot_uuid")
                    or current[identity].get("uuid") != byName[identity]["snapshot_uuid"] for identity in protected):
                raise ValueError("Three protected complete verified native recovery points no longer exist")
            if name not in current:
                deleted.append(name)
                continue
            if current[name].get("uuid") != byName[name].get("snapshot_uuid"):
                raise ValueError("A snapshot identity changed before native retention deletion")
            result: dict[str, object] = _request(client, "DELETE", f"/_snapshot/knowhere-full/{name}")
            if result.get("acknowledged") is not True:
                raise ValueError("Native snapshot deletion was not acknowledged")
            deleted.append(name)
        return {**plan, "deleted": deleted}
