"""Bounded incremental campaign counters and actual SQL/native resource samples."""

from datetime import datetime, timezone
from decimal import Decimal
from collections.abc import Iterator
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import stat
from typing import cast
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from shared.core.config import settings
from shared.models.database.indexing import IndexingAttempt
from shared.models.database.job import Job
from shared.models.database.lexicalbuild import LexicalBuild
from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
from shared.services.retrieval.lexical.coordination import LexicalCoordination
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from shared.services.retrieval.lexical.scope import LexicalScope

_STAGES: frozenset[str] = frozenset(("pg_pool_wait_ms", "pg_transaction_ms", "native_http_pool_wait_ms",
    "native_response_bytes", "native_response_processing_ms", "inline_indexing_ms", "manual_retry_ms", "bulk_ms", "visibility_ms"))
_REQUIRED: frozenset[str] = _STAGES | frozenset(("pg_lock_wait_sampled_lower_bound_ms", "native_connections", "native_task_queue", "native_rejections",
    "cpu_percent", "heap_used_bytes", "gc_collection_time_ms", "disk_free_bytes", "open_indexes", "reserved_indexes", "uncertain_indexes",
    "catalog_indexes", "parse_task_backlog", "publication_age_ms", "pg_copy_age_ms"))
_MEASUREMENT_KINDS: dict[str, str] = {name: "gauge" for name in _REQUIRED} | {
    name: "cumulative-counter" for name in _STAGES | {"native_rejections"}} | {
    "pg_lock_wait_sampled_lower_bound_ms": "sampled-lower-bound", "gc_collection_time_ms": "collection-time-proxy"}


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Resource evidence requires a JSON object")
    return cast(dict[str, object], value)


def _number(value: object) -> float:
    if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool) or not isfinite(value) or value < 0:
        raise ValueError("Resource evidence requires an actual finite numeric measurement")
    return float(value)


def _writePrivate(path: Path, value: dict[str, object]) -> None:
    temporary: Path = path.with_name(path.name + "." + uuid4().hex)
    try:
        descriptor: int = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, separators=(",", ":"), allow_nan=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class WorkloadResourceSampler:
    def __init__(self, *, configuration: dict[str, object], sessionFactory: sessionmaker[Session]) -> None:
        self.configuration: dict[str, object] = configuration
        self.resources: dict[str, object] = _object(configuration["resources"])
        self.sessions: sessionmaker[Session] = sessionFactory

    def _rows(self, path: Path, *, offset: int = 0) -> Iterator[tuple[dict[str, object], int]]:
        descriptor: int = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                or metadata.st_size > int(str(self.resources["maximum_journal_bytes"])) or offset > metadata.st_size):
                raise ValueError("Resource journal is not private, bounded or append-only")
            stream.seek(offset)
            while stream.tell() < metadata.st_size:
                line: bytes = stream.readline(8193)
                if not line:
                    break
                if len(line) > 8192 or not line.endswith(b"\n"):
                    raise ValueError("Resource journal contains an incomplete or oversized record")
                row = _object(json.loads(line))
                if (row.get("mode"), row.get("campaign_id"), row.get("source_hash")) != (
                    "actual", self.configuration["campaign_id"], self.configuration["source_hash"]):
                    continue
                yield row, stream.tell()

    def _readCounters(self) -> tuple[dict[str, float], dict[str, object]]:
        statePath: object = self.resources.get("state_path")
        if not isinstance(statePath, str):
            raise ValueError("Resource sampling requires an explicit private durable cursor path")
        path: Path = Path(statePath)
        from shared.services.retrieval.lexical.workload_probe import _readPrivate
        state: dict[str, object] = _readPrivate(statePath, 1024 * 1024) if path.exists() else {}
        if state and (state.get("campaign_id"), state.get("source_hash")) != (self.configuration["campaign_id"], self.configuration["source_hash"]):
            raise ValueError("Resource cursor belongs to another campaign/source")
        offsets = _object(state.get("offsets", {}))
        totals = {key: _number(value) for key, value in _object(state.get("totals", {})).items() if key in _STAGES}
        latest: str | None = cast(str | None, state.get("latest_observed_at_utc"))
        journalPaths = self.resources["journal_paths"]
        if not isinstance(journalPaths, list) or not journalPaths:
            raise ValueError("Resource sampling requires actual observer journals")
        for journal in journalPaths:
            journalPath: Path = Path(str(journal))
            for row, offset in self._rows(journalPath, offset=int(str(offsets.get(str(journal), 0)))):
                offsets[str(journal)] = offset
                observed: object = row.get("observed_at_utc")
                if not isinstance(observed, str):
                    raise ValueError("Stage observation has no actual timestamp")
                latest = max(latest or observed, observed)
                if row.get("kind") == "observer_install":
                    capabilities = row.get("capabilities")
                    if isinstance(capabilities, list):
                        for metric in capabilities:
                            if metric in _STAGES:
                                totals.setdefault(str(metric), 0.0)
                for metric in _STAGES:
                    if metric in row:
                        totals[metric] = totals.get(metric, 0) + _number(row[metric])
        if latest is None:
            raise ValueError("No actual campaign observer installation or stage records exist")
        observedAt = datetime.fromisoformat(latest)
        if observedAt.tzinfo is None:
            raise ValueError("Actual stage observations require an aware UTC clock")
        age: float = (datetime.now(timezone.utc) - observedAt).total_seconds()
        if age < 0 or age > int(str(self.resources["freshness_seconds"])):
            raise ValueError("Actual stage observations are stale or from an invalid clock")
        state.update({"campaign_id": self.configuration["campaign_id"], "source_hash": self.configuration["source_hash"],
            "offsets": offsets, "totals": totals, "latest_observed_at_utc": latest})
        _writePrivate(path, state)
        return dict(totals), state

    def sample(self) -> dict[str, object]:
        metrics, state = self._readCounters()
        cases = _object(self.configuration["cases"])
        if not cases:
            raise ValueError("Resource sample needs an approved native scope")
        case = _object(next(iter(cases.values())))
        native = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
            scope=LexicalScope(ownerId=str(case["owner_id"]), namespace=str(case["namespace"])),
            indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX)
        try:
            live = native.getLifecycleSnapshot(isExact=False, timeoutSeconds=5)
            body = _object(native._request("GET", "/_nodes/stats/os,jvm,fs,http,thread_pool"))
            nodes = _object(body.get("nodes"))
            if len(nodes) != 1 or set(nodes) != set(live.nodes):
                raise ValueError("Resource sample must identify the actual single native node")
            node = _object(next(iter(nodes.values())))
            osMetrics = _object(node.get("os"))
            jvm = _object(node.get("jvm"))
            pools = _object(node.get("thread_pool"))
            collectors = _object(_object(jvm.get("gc")).get("collectors"))
            if not pools or not collectors:
                raise ValueError("Native task queues/rejections or JVM collection counters are unavailable")
            indexNames: list[str] = [name for name in live.metadata.indices if name.startswith(settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX + "-")]
            metrics.update({"cpu_percent": _number(_object(osMetrics.get("cpu")).get("percent")),
                "heap_used_bytes": _number(_object(jvm.get("mem")).get("heap_used_in_bytes")),
                "gc_collection_time_ms": sum(_number(_object(value).get("collection_time_in_millis")) for value in collectors.values()),
                "disk_free_bytes": _number(_object(_object(node.get("fs")).get("total")).get("available_in_bytes")),
                "native_connections": _number(_object(node.get("http")).get("current_open")),
                "native_task_queue": sum(_number(_object(value).get("queue")) for value in pools.values()),
                "native_rejections": sum(_number(_object(value).get("rejected")) for value in pools.values()),
                "catalog_indexes": float(len(indexNames)), "open_indexes": float(sum(live.isOpen(name) for name in indexNames))})
            with self.sessions.begin() as db:
                control = LexicalCoordination.lockControl(db)
                if (control.clusterUuid, control.nodeIncarnation) != (live.cluster_uuid, live.getIncarnation()):
                    raise ValueError("Native resource target differs from actual SQL identity")
                counts: dict[str, int] = {str(row[0]): int(row[1]) for row in db.execute(
                    select(LexicalNamespaceIndex.state, func.count()).group_by(LexicalNamespaceIndex.state))}
                metrics.update({"reserved_indexes": float(counts.get("opening", 0) + counts.get("closing", 0)),
                    "uncertain_indexes": float(counts.get("uncertain", 0))})
                publicationAge = db.scalar(select(func.coalesce(func.extract("epoch", func.clock_timestamp() - func.min(IndexingAttempt.createdAt)) * 1000, 0))
                    .join(Job, Job.current_indexing_attempt_id == IndexingAttempt.id).where(Job.job_metadata["mixed_workload_campaign"].as_string() == self.configuration["campaign_id"],
                        Job.status.in_(("pending", "running"))))
                copyAge = db.scalar(select(func.coalesce(func.extract("epoch", func.clock_timestamp() - func.min(LexicalBuild.createdAt)) * 1000, 0))
                    .where(LexicalBuild.legacyCopyState == "pending"))
                metrics.update({"publication_age_ms": _number(publicationAge), "pg_copy_age_ms": _number(copyAge)})
                now: float = float(db.scalar(select(func.extract("epoch", func.clock_timestamp()))) or 0)
                locked: list[str] = [str(row[0]) + ":" + str(row[1]) for row in db.execute(text(
                    "SELECT pid,query_start FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'"))]
            previous: object = state.get("lock_observation")
            interval: float = 0
            if isinstance(previous, dict):
                elapsed: float = now - _number(previous.get("database_time"))
                if 0 <= elapsed <= int(str(self.resources["freshness_seconds"])):
                    previousLocks = previous.get("locks")
                    interval = elapsed * 1000 * len(set(locked) & set(previousLocks if isinstance(previousLocks, list) else []))
            state["lock_observation"] = {"database_time": now, "locks": locked}
            state["sampled_lock_wait_ms"] = _number(state.get("sampled_lock_wait_ms", 0)) + interval
            metrics["pg_lock_wait_sampled_lower_bound_ms"] = _number(state["sampled_lock_wait_ms"])
            _writePrivate(Path(str(self.resources["state_path"])), state)
            brokerPath: object = self.resources.get("broker_url_file")
            queueName: object = self.resources.get("parse_queue_name")
            if isinstance(brokerPath, str) and isinstance(queueName, str):
                from kombu.transport.redis import Channel
                from redis import Redis
                from shared.core.celery_app import celery_app
                path = Path(brokerPath)
                metadata = path.stat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 65536:
                    raise ValueError("Broker input must be a private bounded secret file")
                options = celery_app.conf.broker_transport_options or {}
                prefix: str = str(options.get("global_keyprefix", ""))
                separator: str = str(options.get("sep", Channel.sep))
                steps: tuple[int, ...] = tuple(options.get("priority_steps", Channel.priority_steps))
                if not steps or len(steps) > 32:
                    raise ValueError("Unsupported Redis priority queue configuration")
                with Redis.from_url(path.read_text().strip(), socket_timeout=5, socket_connect_timeout=5) as redis:
                    with redis.pipeline(transaction=False) as pipe:
                        for priority in steps:
                            pipe.llen(prefix + queueName + (separator + str(priority) if priority else ""))
                        metrics["parse_task_backlog"] = sum(_number(length) for length in pipe.execute())
            missing: list[str] = sorted(_REQUIRED - metrics.keys())
            return {"complete": not missing, "metrics": metrics, "missing_measurements": missing,
                "measurement_semantics": "completed-stage cumulative milliseconds/bytes; JVM cumulative collection milliseconds; sampled lock-wait lower bound; SQL/native instantaneous gauges",
                "measurement_kinds": {name: _MEASUREMENT_KINDS[name] for name in metrics if name in _MEASUREMENT_KINDS},
                "missing_exact_measurements": ["pg_lock_wait", "gc_stop_the_world_pause"],
                "cluster_uuid": live.cluster_uuid, "node_incarnation": live.getIncarnation()}
        finally:
            native.close()

    def export(self) -> dict[str, object]:
        output: object = self.resources.get("export_path")
        journals = self.resources["journal_paths"]
        if not isinstance(output, str) or not isinstance(journals, list):
            raise ValueError("Stage export requires an explicit new private output path and journals")
        path: Path = Path(output)
        if path.resolve() in {Path(str(journal)).resolve() for journal in journals}:
            raise ValueError("Stage export cannot overwrite an observer journal")
        count: int = 0
        digest = sha256()
        descriptor: int = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            for journal in journals:
                for row, _offset in self._rows(Path(str(journal))):
                    if row.get("kind") not in ("read", "write"):
                        continue
                    data: bytes = (json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n").encode()
                    stream.write(data)
                    digest.update(data)
                    count += 1
        return {"telemetry_path": str(path), "telemetry_sha256": digest.hexdigest(), "records": count,
            "complete": count > 0}
