"""Independent off-node checks using the existing Loguru/Logfire error path.

Run from an approved operational host, outside parsing/indexing worker pools.
The default prints a dry-run; --execute reads native health and aggregate metrics.
Sending into the existing Logfire project additionally requires the explicit
--send-existing-logfire flag and its mounted token. No alert switches a backend,
changes a job, sends a message directly, or proves notification delivery.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import cast

import httpx

from operations import OpenSearchOperations


class NativeMonitor:
    @staticmethod
    def observeBackup(journal: dict[str, object], *, nowSeconds: int) -> dict[str, object]:
        backup: object = journal.get("backup")
        if not isinstance(backup, dict) or not isinstance(backup.get("points"), list):
            raise ValueError("Backup monitor requires the authoritative workflow journal")
        points: list[dict[str, object]] = cast(list[dict[str, object]], backup["points"])
        verified: list[dict[str, object]] = [point for point in points if point.get("state") == "SUCCESS"
            and point.get("data_verified") is True and point.get("coverage_verified") is True
            and isinstance(point.get("independent_verification_sha256"), str)]
        newest: dict[str, object] = max(verified, key=lambda point: int(str(point["completed_at_millis"])), default={})
        latest: dict[str, object] = max(points, key=lambda point: int(str(point["completed_at_millis"])), default={})
        pending: object = backup.get("pending")
        completion: int | None = (nowSeconds * 1000 - int(str(newest["completed_at_millis"]))) if newest else None
        boundary: int | None = (nowSeconds * 1000 - int(str(newest["data_boundary_captured_at_millis"]))) if newest else None
        if (completion is not None and completion < -1000) or (boundary is not None and boundary < -1000):
            raise ValueError("Backup completion/boundary clock is inconsistent")
        return {"latest_complete_snapshot_age_seconds": max(0, completion // 1000) if completion is not None else None,
            "backup_incomplete": not newest or latest.get("snapshot_uuid") != newest.get("snapshot_uuid")
                or isinstance(pending, dict) and pending.get("phase") not in ("snapshot-submitted",),
            "latest_snapshot_duration_millis": newest.get("duration_millis"),
            "latest_data_boundary_age_seconds": max(0, boundary // 1000) if boundary is not None else None,
            "latest_point_missing_current_build_count": len(cast(list[object], newest.get("missing_current_builds", [])))}

    @staticmethod
    def observeApplication() -> dict[str, object]:
        root: Path = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / "packages/shared-python"))
        from sqlalchemy import func, select, text
        from shared.core.database_sync import get_sync_session_factory
        from shared.models.database.indexing import IndexingAttempt
        from shared.models.database.job import Job
        from shared.models.database.lexicalbuild import LexicalBuild
        from shared.models.database.lexicalnamespace import LexicalNamespaceIndex

        with get_sync_session_factory().begin() as database:
            database.execute(text("SET TRANSACTION READ ONLY"))
            database.execute(text("SET LOCAL statement_timeout = '5s'"))
            if not database.scalar(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")):
                raise ValueError("Off-node age sampling requires approved full maintenance visibility")
            publication = database.scalar(select(func.coalesce(func.extract(
                "epoch", func.clock_timestamp() - func.min(IndexingAttempt.createdAt)), 0)).join(
                    Job, Job.current_indexing_attempt_id == IndexingAttempt.id).where(Job.indexing_status.in_(("pending", "running"))))
            copy = database.scalar(select(func.coalesce(func.extract(
                "epoch", func.clock_timestamp() - func.min(LexicalBuild.createdAt)), 0)).where(LexicalBuild.legacyCopyState == "pending"))
            counts: dict[str, int] = {str(state): int(count) for state, count in database.execute(
                select(LexicalNamespaceIndex.state, func.count()).group_by(LexicalNamespaceIndex.state))}
            return {"indexing_oldest_seconds": max(0, int(publication or 0)), "pg_copy_oldest_seconds": max(0, int(copy or 0)),
                "lifecycle_counts": counts}

    @staticmethod
    def check(client: httpx.Client | None, metrics: object, *, areMetricsFresh: bool,
            previous: dict[str, object], nowSeconds: int, repeatSeconds: int) -> dict[str, object]:
        isAvailable: bool = False
        if client is not None:
            try:
                response: httpx.Response = client.get("/_cluster/health?timeout=2s")
                response.raise_for_status()
                health: object = response.json()
                isAvailable = isinstance(health, dict) and health.get("status") in ("green", "yellow") and health.get("timed_out") is False
            except (httpx.HTTPError, ValueError):
                pass
        defaults: dict[str, object] = {
            "indexing_oldest_seconds": 0, "pg_copy_oldest_seconds": 0,
            "latest_complete_snapshot_age_seconds": None, "backup_incomplete": True,
        }
        payload: dict[str, object] = {**defaults, **(cast(dict[str, object], metrics) if isinstance(metrics, dict) else {})}
        payload["native_available"] = isAvailable
        try:
            resourceValue: object = payload.get("resource_pressure", {})
            if not isinstance(resourceValue, dict):
                raise ValueError("Resource metrics must be an object")
            resource: dict[str, object] = cast(dict[str, object], resourceValue)
            payload["resource_pressure"] = {**resource, "operational-metrics-stale": not areMetricsFresh}
            result: dict[str, object] = OpenSearchOperations.evaluateAlerts(payload, previous, nowSeconds=nowSeconds, repeatSeconds=repeatSeconds)
        except ValueError:
            areMetricsFresh = False
            result = OpenSearchOperations.evaluateAlerts({**defaults, "native_available": isAvailable,
                "resource_pressure": {"operational-metrics-stale": True}}, previous, nowSeconds=nowSeconds, repeatSeconds=repeatSeconds)
        # A missing source is an operational fault, not a qualified resource warning.
        for event in cast(list[dict[str, object]], result["events"]):
            if event["condition"] == "resource-operational-metrics-stale":
                event["condition"] = "operational-metrics-stale"
        result["native_available"] = isAvailable
        result["aggregate_metrics_fresh"] = areMetricsFresh
        return result


def _main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--metrics", required=True, type=Path)
    parser.add_argument("--backup-journal", type=Path)
    parser.add_argument("--sample-application", action="store_true")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--metrics-max-age-seconds", required=True, type=int)
    parser.add_argument("--repeat-seconds", required=True, type=int)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--send-existing-logfire", action="store_true")
    arguments: argparse.Namespace = parser.parse_args()
    if arguments.metrics_max_age_seconds < 1:
        raise ValueError("Declare a positive aggregate-metrics freshness bound")
    if not arguments.execute:
        print(json.dumps({"mode": "dry-run", "runs_outside_worker_and_native_host": True,
            "native_read": "GET /_cluster/health?timeout=2s", "metrics": str(arguments.metrics),
            "backup_journal": str(arguments.backup_journal) if arguments.backup_journal else None,
            "queries_actual_publication_and_copy_age": arguments.sample_application,
            "sends_to_existing_logfire": arguments.send_existing_logfire, "mutates_job_or_backend": False}))
        return 0
    from manage import _nativeClient, _readJson, _requireObject, _writeJson

    now: int = int(datetime.now(timezone.utc).timestamp())
    metrics: object = None
    isFresh: bool = False
    try:
        metrics = _readJson(arguments.metrics)
        observedAt: int = int(str(_requireObject(metrics).get("observed_at_seconds", 0)))
        isFresh = (0 <= now - observedAt <= arguments.metrics_max_age_seconds
            and 0 <= now - arguments.metrics.stat().st_mtime <= arguments.metrics_max_age_seconds)
    except (ValueError, OSError):
        pass
    supplemental: dict[str, object] = {}
    supplementalErrors: list[str] = []
    if arguments.backup_journal is not None:
        try:
            supplemental.update(NativeMonitor.observeBackup(_requireObject(_readJson(arguments.backup_journal)), nowSeconds=now))
        except (ValueError, KeyError, OSError):
            supplemental.update({"backup_incomplete": True, "latest_complete_snapshot_age_seconds": None})
            supplementalErrors.append("authoritative-backup-journal-unavailable")
    if arguments.sample_application:
        try:
            supplemental.update(NativeMonitor.observeApplication())
        except Exception:
            supplementalErrors.append("authoritative-publication-age-unavailable")
    baseMetrics: dict[str, object] = cast(dict[str, object], metrics) if isinstance(metrics, dict) else {}
    metrics = {**baseMetrics, **{key: value for key, value in supplemental.items() if key in (
        "latest_complete_snapshot_age_seconds", "backup_incomplete", "indexing_oldest_seconds", "pg_copy_oldest_seconds")}}
    isFresh = isFresh and not supplementalErrors
    previous: dict[str, object] = _requireObject(_readJson(arguments.state)) if arguments.state.exists() else {}
    try:
        with _nativeClient(_requireObject(_readJson(arguments.manifest))) as client:
            result: dict[str, object] = NativeMonitor.check(client, metrics, areMetricsFresh=isFresh,
                previous=previous, nowSeconds=now, repeatSeconds=arguments.repeat_seconds)
    except (ValueError, OSError):
        # Invalid/missing client trust is itself observable without making network I/O.
        result = NativeMonitor.check(None, metrics, areMetricsFresh=isFresh,
            previous=previous, nowSeconds=now, repeatSeconds=arguments.repeat_seconds)
    from loguru import logger

    if arguments.send_existing_logfire:
        if not os.environ.get("LOGFIRE_TOKEN"):
            raise ValueError("Use the existing approved Logfire token for the off-node alert path")
        import logfire

        logfire.configure(service_name="knowhere-native-monitor", token=os.environ["LOGFIRE_TOKEN"], console=False)
        logger.add(**logfire.loguru_handler(), level="INFO")
    for event in cast(list[dict[str, object]], result["events"]):
        logger.bind(event="app.log", component="native-offnode-monitor", **event).log(
            "INFO" if event["transition"] == "resolved" else "ERROR",
            "Native operational condition {condition}: {transition}",
            condition=event["condition"], transition=event["transition"],
        )
    if arguments.send_existing_logfire:
        import logfire

        logfire.force_flush()
    _writeJson(arguments.state, result["state"], replace=arguments.state.exists())
    result["source_measurements"] = supplemental
    result["source_measurement_errors"] = supplementalErrors
    print(json.dumps({key: value for key, value in result.items() if key != "state"}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except (ValueError, OSError):
        print("Native off-node monitor configuration or aggregate input failed.", file=sys.stderr)
        raise SystemExit(2) from None
