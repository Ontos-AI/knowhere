"""Concurrent publication batches with protected retrieval and resource evidence."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
import os
import subprocess
from threading import Barrier, Event
from time import monotonic
from typing import Any, Mapping, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from scripts.publication_benchmark.publication_execution import (
    PublicationExecutionRequest,
    PublicationScope,
    apply_publication_environment,
    database_url_for_owner,
    executor_for,
)
from scripts.publication_benchmark.report import percentile
from scripts.publication_benchmark.retrieval_interference import (
    RetrievalProbeSpec,
    evaluate_probe_samples,
    probe_samples_cover_publication,
    run_open_loop_probes,
)
from scripts.publication_benchmark.state_snapshot import opaque_reference


@dataclass(frozen=True)
class CapacityBatch:
    """One fresh-clone batch; identifiers remain local to the database."""

    database_url: str
    run_id: str
    owner: str
    strategy: str
    concurrency: int
    chunks: Sequence[Mapping[str, Any]]
    probe_spec: RetrievalProbeSpec
    probe_rate: int
    probe_duration_seconds: float
    container_name: str | None = None


def _database_resources(database_url: str, spec: RetrievalProbeSpec) -> dict[str, int]:
    engine = create_engine(database_url_for_owner(database_url, owner="sync"))
    try:
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT pg_current_wal_lsn()::text AS wal_position, "
                        "(SELECT temp_bytes FROM pg_stat_database WHERE datname = current_database()) "
                        "AS temp_bytes, (SELECT deadlocks FROM pg_stat_database "
                        "WHERE datname = current_database()) AS deadlocks, "
                        "(SELECT checkpoints_timed + checkpoints_req FROM pg_stat_bgwriter) "
                        "AS checkpoints, pg_database_size(current_database()) AS database_bytes, "
                        "(SELECT coalesce(sum(pg_relation_size(c.oid)), 0) FROM pg_class c "
                        "WHERE c.relkind IN ('r', 'm') AND c.relnamespace IN "
                        "(SELECT oid FROM pg_namespace WHERE nspname = 'public')) AS table_bytes, "
                        "(SELECT coalesce(sum(pg_relation_size(c.oid)), 0) FROM pg_class c "
                        "WHERE c.relkind = 'i' AND c.relnamespace IN "
                        "(SELECT oid FROM pg_namespace WHERE nspname = 'public')) AS index_bytes, "
                        "(SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()) "
                        "AS connections, "
                        "(SELECT generation FROM retrieval_namespace_generations "
                        "WHERE user_id = :user_id AND namespace = :namespace) AS generation"
                    ),
                    {"user_id": spec.user_id, "namespace": spec.namespace},
                )
                .mappings()
                .one()
            )
            measured = {
                key: int(value or 0)
                for key, value in row.items()
                if key != "wal_position"
            }
            high, low = str(row["wal_position"]).split("/", 1)
            measured["wal_position"] = int(high, 16) * (2**32) + int(low, 16)
            return measured
    finally:
        engine.dispose()


def _database_index_sizes(database_url: str) -> dict[str, int]:
    """Return physical sizes for public indexes, keyed by relation name."""
    engine = create_engine(database_url_for_owner(database_url, owner="sync"))
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT n.nspname || '.' || c.relname AS index_name, "
                    "pg_relation_size(c.oid) AS size_bytes "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relkind = 'i' AND n.nspname = 'public' "
                    "ORDER BY index_name"
                )
            ).all()
            return {str(row[0]): int(row[1] or 0) for row in rows}
    finally:
        engine.dispose()


def _container_resources(container_name: str | None) -> dict[str, int | None]:
    if not container_name:
        return {
            "peak_rss_bytes": None,
            "cpu_microseconds": None,
            "free_disk_bytes": None,
        }
    readings: dict[str, int | None] = {}
    for key, command in (
        ("peak_rss_bytes", "cat /sys/fs/cgroup/memory.peak"),
        ("cpu_microseconds", "awk '/usage_usec/ {print $2}' /sys/fs/cgroup/cpu.stat"),
        ("free_disk_bytes", "df -B 1 /var/lib/postgresql/data | awk 'NR==2 {print $4}'"),
    ):
        completed = subprocess.run(
            ["docker", "exec", container_name, "sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        try:
            readings[key] = (
                int(completed.stdout.strip()) if completed.returncode == 0 else None
            )
        except ValueError:
            readings[key] = None
    return readings


def _monitor_peak_connections(database_url: str, stop_event: Event) -> int:
    """Observe PostgreSQL connection use throughout the publication window."""
    engine = create_engine(database_url_for_owner(database_url, owner="sync"))
    peak = 0
    try:
        with engine.connect() as connection:
            while True:
                observed = int(
                    connection.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                        )
                    ).scalar_one()
                )
                peak = max(peak, observed)
                connection.rollback()
                if stop_event.wait(0.1):
                    break
        return peak
    finally:
        engine.dispose()


def _check_convergence(
    batch: CapacityBatch, scopes: Sequence[PublicationScope], starting_generation: int
) -> dict[str, bool]:
    from shared.services.retrieval.serving_manifest import decode_namespace_map_snapshot

    engine = create_engine(database_url_for_owner(batch.database_url, owner="sync"))
    try:
        with Session(engine) as session:
            revisions = session.execute(
                text(
                    "SELECT document_id, current_job_result_id FROM documents "
                    "WHERE user_id = :user_id AND namespace = :namespace "
                    "AND current_job_result_id = ANY(:revisions) AND status = 'active'"
                ),
                {
                    "user_id": batch.probe_spec.user_id,
                    "namespace": batch.probe_spec.namespace,
                    "revisions": [scope.revision_ref for scope in scopes],
                },
            ).all()
            revision_by_document = {str(row[0]): str(row[1]) for row in revisions}
            row = (
                session.execute(
                    text(
                        "SELECT s.payload_zlib, s.checksum, s.format_version, s.generation "
                        "FROM retrieval_namespace_map_snapshots s "
                        "WHERE s.user_id = :user_id AND s.namespace = :namespace"
                    ),
                    {
                        "user_id": batch.probe_spec.user_id,
                        "namespace": batch.probe_spec.namespace,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return {
                    "all_revisions_active": False,
                    "snapshot_converged": False,
                    "graph_converged": False,
                    "generation_advanced": False,
                }
            payload = decode_namespace_map_snapshot(
                row["payload_zlib"],
                checksum=str(row["checksum"]),
                format_version=int(row["format_version"]),
            )
            snapshot_documents = payload.get("documents") or {}
            graph_count = int(
                session.execute(
                    text(
                        "SELECT count(*) FROM graph_nodes WHERE user_id = :user_id "
                        "AND namespace = :namespace AND owner_document_id = ANY(:document_ids)"
                    ),
                    {
                        "user_id": batch.probe_spec.user_id,
                        "namespace": batch.probe_spec.namespace,
                        "document_ids": list(revision_by_document),
                    },
                ).scalar_one()
            )
            return {
                "all_revisions_active": len(revision_by_document) == len(scopes),
                "snapshot_converged": len(revision_by_document) == len(scopes)
                and all(
                    snapshot_documents.get(document_id, {}).get("job_result_id")
                    == revision_id
                    for document_id, revision_id in revision_by_document.items()
                ),
                "graph_converged": graph_count == len(scopes),
                "generation_advanced": int(row["generation"])
                == starting_generation + len(scopes),
            }
    finally:
        engine.dispose()


def run_capacity_batch(batch: CapacityBatch) -> dict[str, Any]:
    """Run one concurrent batch and probe the unchanged protected corpus."""
    if batch.concurrency < 1 or not batch.chunks:
        raise ValueError("capacity batch requires concurrency and frozen chunks")
    apply_publication_environment(
        database_url=database_url_for_owner(batch.database_url, owner="async"),
        run_id=batch.run_id,
        strategy=batch.strategy,
    )
    from shared.core.config import settings

    settings.KNOWHERE_PUBLICATION_STRATEGY = batch.strategy
    os.environ["KNOWHERE_PUBLICATION_STRATEGY"] = batch.strategy
    quiet_samples = asyncio.run(
        run_open_loop_probes(
            database_url=batch.database_url,
            spec=batch.probe_spec,
            rate_per_second=batch.probe_rate,
            duration_seconds=1.0,
        )
    )
    quiet_gate = evaluate_probe_samples(quiet_samples)
    if quiet_gate["status"] != "pass":
        raise RuntimeError("protected retrieval quiet baseline failed")

    before = _database_resources(batch.database_url, batch.probe_spec)
    index_sizes_before = _database_index_sizes(batch.database_url)
    container_before = _container_resources(batch.container_name)
    suffix = sha256(batch.run_id.encode()).hexdigest()[:10]
    scopes = [
        PublicationScope(
            user_ref=batch.probe_spec.user_id,
            namespace_ref=batch.probe_spec.namespace,
            source_file_name=f"capacity-{suffix}-{index}.pdf",
            job_ref=f"cap-job-{suffix}-{index}",
            revision_ref=f"cap-result-{suffix}-{index}",
        )
        for index in range(batch.concurrency)
    ]
    barrier = Barrier(batch.concurrency)

    def publish(index: int) -> dict[str, Any]:
        scope = scopes[index]
        request = PublicationExecutionRequest(
            database_url=database_url_for_owner(batch.database_url, owner=batch.owner),
            scope=scope,
            chunks=batch.chunks,
            owner=batch.owner,
            mode="capacity",
            redis_namespace=f"publication-benchmark:{batch.run_id}",
            trace_enabled=True,
            publication_attempt_ref=f"cap-attempt-{suffix}-{index}",
        )
        barrier.wait(timeout=30)
        started_at = monotonic()
        try:
            result = executor_for(batch.owner).execute(request)
            sql = result.terminal_trace.get("sql") if result.terminal_trace else None
            return {
                "outcome": result.outcome,
                "arrival_to_commit_ms": (monotonic() - started_at) * 1000,
                "max_sql_seconds": float(sql.get("max_statement_ms", 0)) / 1000
                if isinstance(sql, Mapping)
                else 0.0,
                "lock_wait_ms": sum(
                    float(result.stage_durations.get(stage, 0.0))
                    for stage in (
                        "namespace_generation_lock_wait",
                        "existing_document_lock_wait",
                    )
                ),
                "error": None,
            }
        except Exception as error:  # noqa: BLE001 - failures are evidence
            return {
                "outcome": "failed",
                "arrival_to_commit_ms": (monotonic() - started_at) * 1000,
                "max_sql_seconds": 0.0,
                "lock_wait_ms": 0.0,
                "error": type(error).__name__,
            }

    publication_done = Event()
    probe_started = Event()
    probe_origin: list[float] = []
    with ThreadPoolExecutor(max_workers=batch.concurrency + 2) as pool:
        probe_future = pool.submit(
            lambda: asyncio.run(
                run_open_loop_probes(
                    database_url=batch.database_url,
                    spec=batch.probe_spec,
                    rate_per_second=batch.probe_rate,
                    duration_seconds=batch.probe_duration_seconds,
                    stop_event=publication_done,
                    started_event=probe_started,
                    origin_capture=probe_origin,
                )
            )
        )
        if not probe_started.wait(timeout=10):
            publication_done.set()
            raise TimeoutError("capacity probe did not start")
        monitor_future = pool.submit(
            _monitor_peak_connections, batch.database_url, publication_done
        )
        started_at = monotonic()
        publication_futures = [
            pool.submit(publish, index) for index in range(batch.concurrency)
        ]
        try:
            publications = [future.result() for future in publication_futures]
        finally:
            publication_ended_at = monotonic()
            publication_done.set()
        publication_seconds = publication_ended_at - started_at
        probe_samples = probe_future.result()
        peak_connections = monitor_future.result()
    after = _database_resources(batch.database_url, batch.probe_spec)
    index_sizes_after = _database_index_sizes(batch.database_url)
    container_after = _container_resources(batch.container_name)
    committed = sum(item["outcome"] == "committed" for item in publications)
    convergence = _check_convergence(batch, scopes, before["generation"])
    probe_gate = evaluate_probe_samples(
        probe_samples,
        expected_result_digest=quiet_gate["result_digest"],
        expected_revision_digest=quiet_gate["revision_digest"],
    )
    resource = {
        "wal_bytes": after["wal_position"] - before["wal_position"],
        "table_size_delta_bytes": after["table_bytes"] - before["table_bytes"],
        "index_size_delta_bytes": after["index_bytes"] - before["index_bytes"],
        "index_size_delta_by_index": {
            index_name: index_sizes_after.get(index_name, 0)
            - index_sizes_before.get(index_name, 0)
            for index_name in sorted(set(index_sizes_before) | set(index_sizes_after))
            if index_sizes_after.get(index_name, 0)
            - index_sizes_before.get(index_name, 0)
            != 0
        },
        "temp_bytes": after["temp_bytes"] - before["temp_bytes"],
        "deadlocks": after["deadlocks"] - before["deadlocks"],
        "checkpoint_pressure": after["checkpoints"] - before["checkpoints"],
        "connection_usage": max(
            before["connections"], peak_connections, after["connections"]
        ),
        "lock_wait_ms": max(
            (float(item.get("lock_wait_ms") or 0) for item in publications),
            default=0.0,
        ),
        "peak_rss_bytes": container_after["peak_rss_bytes"],
        "cpu_seconds": (
            (container_after["cpu_microseconds"] - container_before["cpu_microseconds"])
            / 1e6
            if container_after["cpu_microseconds"] is not None
            and container_before["cpu_microseconds"] is not None
            else None
        ),
        "free_disk_bytes": container_after["free_disk_bytes"],
    }
    errors = sum(item["outcome"] != "committed" for item in publications)
    return {
        "schema_version": "publication-capacity-batch/1",
        "owner": batch.owner,
        "strategy": batch.strategy,
        "concurrency": batch.concurrency,
        "errors": errors,
        "timeouts": sum(item["error"] == "TimeoutError" for item in publications),
        "deadlocks": resource["deadlocks"],
        "converged": committed == batch.concurrency and all(convergence.values()),
        "convergence_checks": convergence,
        "throughput_per_second": committed / publication_seconds
        if publication_seconds > 0
        else 0,
        "arrival_to_commit_p95_ms": percentile(
            [item["arrival_to_commit_ms"] for item in publications], 0.95
        ),
        "max_sql_seconds": max(
            (item["max_sql_seconds"] for item in publications), default=0
        ),
        "publication_seconds": publication_seconds,
        "probe_covered_publication": bool(probe_origin)
        and probe_samples_cover_publication(
            probe_samples,
            probe_origin=probe_origin[0],
            publication_started_at=started_at,
            publication_ended_at=publication_ended_at,
        ),
        "probe_gate": probe_gate,
        "probe_p95_ms": percentile(
            [sample.duration_ms for sample in probe_samples], 0.95
        ),
        "probe_sample_count": len(probe_samples),
        "resource": resource,
        "publication_outcomes": [item["outcome"] for item in publications],
        "publication_error_types": [
            item["error"] for item in publications if item["error"]
        ],
        "scope_ref": opaque_reference(batch.probe_spec.namespace),
    }
