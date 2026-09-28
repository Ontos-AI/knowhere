"""Contract tests for conservative telemetry admission evidence."""

# ruff: noqa: E402

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.support.publication_benchmark_support import (
    benchmark_layout,
    ensure_benchmark_import_path,
)

ensure_benchmark_import_path()

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.admit_telemetry import (  # noqa: E402
    evaluate_telemetry_admission,
)
from scripts.publication_benchmark.compare_state import (  # noqa: E402
    state_parity_artifact_path,
)
from scripts.publication_benchmark.check_memory_growth import (  # noqa: E402
    MEASURED_COUNT,
    SCHEMA_VERSION as MEMORY_GROWTH_SCHEMA_VERSION,
    WARMUP_COUNT,
    artifact_path as memory_artifact_path,
    evaluate_memory_series,
)
from scripts.publication_benchmark.check_pool_reuse import (  # noqa: E402
    POOL_REUSE_SCHEMA_VERSION,
)
from scripts.publication_benchmark.state_parity import (  # noqa: E402
    SEMANTIC_COMPONENTS,
    compare_publication_state,
    write_state_parity_artifact,
)
from scripts.publication_benchmark.state_snapshot import (  # noqa: E402
    SEMANTIC_STATE_SCHEMA_VERSION,
    STATE_SNAPSHOT_SCHEMA_VERSION,
)

RUN_ID = "admission-contract-run"
MEMORY_RUN_ID = "admission-small-memory-run"
POOL_RUN_ID = "admission-small-pool-run"
INPUT_DIGEST = "sha256:" + "a" * 64
MEMORY_INPUT_DIGEST = "sha256:" + "b" * 64
SOURCE_CONTENT_DIGEST = "sha256:" + "c" * 64


def write_pair_evidence(
    root: Path,
    *,
    owner: str,
    pair_index: int,
    enabled_duration_ms: float = 102.0,
    write_trace: bool = True,
    trace_outcome: str = "success",
    code_commit: str = "commit",
    source_content_digest: str = SOURCE_CONTENT_DIGEST,
    enabled_source_content_digest: str | None = None,
    sql_counts: tuple[int, int] | None = None,
    enabled_sql_counts: tuple[int, int] | None = None,
) -> None:
    layout = benchmark_layout(root)
    disabled_id = f"disabled-{owner}-{pair_index}"
    enabled_id = f"enabled-{owner}-{pair_index}"
    for sample_id, trace_enabled, duration in (
        (disabled_id, False, 100.0),
        (enabled_id, True, enabled_duration_ms),
    ):
        cli_support.append_jsonl(
            layout.publication_record_path(RUN_ID),
            {
                "run_id": RUN_ID,
                "sample_id": sample_id,
                "owner": owner,
                "strategy": "baseline",
                "mode": "cold",
                "code_commit": code_commit,
                "source_content_digest": (
                    enabled_source_content_digest or source_content_digest
                    if trace_enabled
                    else source_content_digest
                ),
                "dependency_lock_digest": "sha256:lock",
                "postgres_profile": "production",
                "postgres_version": "15",
                "postgres_settings_digest": "sha256:settings",
                "clone_source_digest": "sha256:source",
                "host_id": "host-opaque",
                "redis_namespace": "benchmark-scope",
                "input_digest": INPUT_DIGEST,
                "template_content_digest": "sha256:template",
                "clone_id": RUN_ID,
                "started_at": "2026-09-23T00:00:00Z",
                "ended_at": "2026-09-23T00:00:01Z",
                "publication_attempt_ref": f"attempt-{sample_id}",
                "trace_enabled": trace_enabled,
                "outcome": "committed",
                "publication_duration_ms": duration,
                "counts": {
                    "persisted_document_chunks": 2,
                    "persisted_map_units": 1,
                    "persisted_token_rows": 3,
                },
                **(
                    {
                        "sql_observation": {
                            "statement_count": (enabled_sql_counts or sql_counts)[0]
                            if trace_enabled
                            else sql_counts[0],
                            "write_statement_count": (enabled_sql_counts or sql_counts)[
                                1
                            ]
                            if trace_enabled
                            else sql_counts[1],
                        }
                    }
                    if sql_counts is not None
                    else {}
                ),
            },
        )
    parity_path = state_parity_artifact_path(
        layout=layout,
        run_id=RUN_ID,
        disabled_sample_id=disabled_id,
        enabled_sample_id=enabled_id,
    )
    snapshot = {
        "schema_version": STATE_SNAPSHOT_SCHEMA_VERSION,
        "scope_ref": "scope-1234567890abcdef",
        "source_file_name_refs": ["ref-1234567890abcdef"],
        "relation_counts": {"document_chunks": 2},
        "chunk_type_counts": {"text": 2},
        "namespace_generation": 1,
        "semantic_fingerprints": {
            "schema_version": SEMANTIC_STATE_SCHEMA_VERSION,
            **{component: "sha256:" + "b" * 64 for component in SEMANTIC_COMPONENTS},
        },
    }
    for sample_id in (disabled_id, enabled_id):
        cli_support.write_json(
            layout.clone_directory(RUN_ID) / "samples" / sample_id / "state-after.json",
            snapshot,
        )
    parity_result = compare_publication_state(
        disabled_snapshot=snapshot,
        enabled_snapshot=snapshot,
        disabled_input_digest=INPUT_DIGEST,
        enabled_input_digest=INPUT_DIGEST,
    )
    write_state_parity_artifact(
        path=parity_path,
        result=parity_result,
        sample_refs=(disabled_id, enabled_id),
    )
    if write_trace:
        cli_support.append_jsonl(
            layout.clone_directory(RUN_ID) / "publication-traces.jsonl",
            {
                "attempt_ref": f"attempt-{enabled_id}",
                "outcome": trace_outcome,
                "counts": {"chunks": 2, "map_units": 1, "tokens": 3},
                "sql": {"statement_count": 9},
            },
        )


def gates_by_name(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(gate["gate"]): gate for gate in result["gates"]}


def write_pool_evidence(
    root: Path,
    *,
    code_commit: str = "commit",
    lock_digest: str = "sha256:lock",
    reused_connection: bool = True,
    residual_trace_checkouts: int = 0,
) -> None:
    cli_support.write_json(
        benchmark_layout(root).report_directory(POOL_RUN_ID) / "pool-reuse.json",
        {
            "schema_version": POOL_REUSE_SCHEMA_VERSION,
            "run_id": POOL_RUN_ID,
            "code_commit": code_commit,
            "source_content_digest": SOURCE_CONTENT_DIGEST,
            "dependency_lock_digest": lock_digest,
            "status": "pass",
            "cases": [
                {
                    "owner": owner,
                    "driver": "psycopg2" if owner == "sync" else "asyncpg",
                    "checkout_count": 3,
                    "same_backend_reused": reused_connection,
                    "residual_trace_checkouts": (
                        residual_trace_checkouts if owner == "sync" else 0
                    ),
                    "first_trace_statement_count": 1,
                    "first_trace_statement_count_after_untraced": 1,
                    "first_trace_statement_count_after_reuse": 1,
                    "second_trace_statement_count": 1,
                }
                for owner in ("sync", "async")
            ],
        },
    )


def test_pool_admission_requires_matching_revision_and_real_reuse(
    tmp_path: Path,
) -> None:
    write_pair_evidence(tmp_path, owner="sync", pair_index=0)
    write_pool_evidence(tmp_path)
    layout = benchmark_layout(tmp_path)

    valid = evaluate_telemetry_admission(
        layout=layout, run_id=RUN_ID, pool_run_id=POOL_RUN_ID
    )
    assert gates_by_name(valid)["pooled-connection-state"]["status"] == "pass"
    assert valid["pool_run_id"] == POOL_RUN_ID

    write_pool_evidence(tmp_path, lock_digest="sha256:other")
    mismatched = evaluate_telemetry_admission(
        layout=layout, run_id=RUN_ID, pool_run_id=POOL_RUN_ID
    )
    assert (
        gates_by_name(mismatched)["pooled-connection-state"]["status"]
        == "insufficient_evidence"
    )

    write_pool_evidence(tmp_path, code_commit="other")
    wrong_commit = evaluate_telemetry_admission(
        layout=layout, run_id=RUN_ID, pool_run_id=POOL_RUN_ID
    )
    assert (
        gates_by_name(wrong_commit)["pooled-connection-state"]["status"]
        == "insufficient_evidence"
    )

    write_pool_evidence(tmp_path, reused_connection=False)
    no_reuse = evaluate_telemetry_admission(
        layout=layout, run_id=RUN_ID, pool_run_id=POOL_RUN_ID
    )
    assert (
        gates_by_name(no_reuse)["pooled-connection-state"]["status"]
        == "insufficient_evidence"
    )


def test_pool_admission_fails_on_observed_leakage(tmp_path: Path) -> None:
    write_pair_evidence(tmp_path, owner="sync", pair_index=0)
    write_pool_evidence(tmp_path, residual_trace_checkouts=1)

    result = evaluate_telemetry_admission(
        layout=benchmark_layout(tmp_path), run_id=RUN_ID, pool_run_id=POOL_RUN_ID
    )

    gate = gates_by_name(result)["pooled-connection-state"]
    assert gate["status"] == "fail"
    assert gate["details"]["leaked_owners"] == ["sync"]
    assert result["status"] == "fail"


def write_memory_arm(
    root: Path,
    *,
    owner: str,
    trace_enabled: bool,
    growth_per_iteration: int = 0,
    input_digest: str = MEMORY_INPUT_DIGEST,
) -> None:
    layout = benchmark_layout(root)
    arm_number = (0 if owner == "sync" else 2) + int(trace_enabled)
    values = [
        300 * 1024 * 1024 + index * growth_per_iteration
        for index in range(MEASURED_COUNT)
    ]
    observations = [
        {
            "iteration": index,
            "phase": "warmup" if index < WARMUP_COUNT else "measured",
            "outcome": "committed",
            "persisted_chunks": 2 * (index + 1),
            "persisted_map_units": index + 1,
            "submitted_chunks": 2,
            "sql_statement_count": 1,
            "has_publication_stages": trace_enabled,
            "terminal_count": int(trace_enabled),
            "terminal_matches_attempt": trace_enabled,
            "terminal_chunk_count": 2 if trace_enabled else 0,
            "database_size_after_bytes": 1024,
            "increased_persisted_state": True,
            "rss_after_gc_bytes": (
                values[0] if index < WARMUP_COUNT else values[index - WARMUP_COUNT]
            ),
        }
        for index in range(WARMUP_COUNT + MEASURED_COUNT)
    ]
    artifact: dict[str, Any] = {
        "schema_version": MEMORY_GROWTH_SCHEMA_VERSION,
        "run_id": MEMORY_RUN_ID,
        "owner": owner,
        "trace_enabled": trace_enabled,
        "strategy": "baseline",
        "code_commit": "commit",
        "source_content_digest": SOURCE_CONTENT_DIGEST,
        "dependency_lock_digest": "sha256:lock",
        "input_digest": input_digest,
        "input_chunk_count": 2,
        "initial_database_bytes": 1024,
        "schema_revision": "20260305_baseline",
        "postgres_profile": "production",
        "postgres_version": "15",
        "postgres_settings_digest": "sha256:settings",
        "clone_source_digest": "sha256:source",
        "process_id": 4000 + arm_number,
        "series_started_at": f"2026-09-23T00:00:0{arm_number}Z",
        "observations": observations,
    }
    artifact["gate"] = evaluate_memory_series(artifact)
    cli_support.write_json(
        memory_artifact_path(
            layout,
            run_id=MEMORY_RUN_ID,
            owner=owner,
            trace_enabled=trace_enabled,
        ),
        artifact,
    )


def test_memory_admission_requires_four_matching_process_series(
    tmp_path: Path,
) -> None:
    write_pair_evidence(tmp_path, owner="sync", pair_index=0)
    for owner in ("sync", "async"):
        for trace_enabled in (False, True):
            write_memory_arm(tmp_path, owner=owner, trace_enabled=trace_enabled)

    complete = evaluate_telemetry_admission(
        layout=benchmark_layout(tmp_path), run_id=RUN_ID, memory_run_id=MEMORY_RUN_ID
    )
    assert gates_by_name(complete)["memory-growth"]["status"] == "pass"
    assert complete["memory_run_id"] == MEMORY_RUN_ID
    assert (
        gates_by_name(complete)["memory-growth"]["details"]["memory_input_digest"]
        == MEMORY_INPUT_DIGEST
    )

    write_memory_arm(
        tmp_path, owner="async", trace_enabled=True, input_digest="sha256:other"
    )
    mismatch = evaluate_telemetry_admission(
        layout=benchmark_layout(tmp_path), run_id=RUN_ID, memory_run_id=MEMORY_RUN_ID
    )
    assert gates_by_name(mismatch)["memory-growth"]["status"] == "insufficient_evidence"

    write_memory_arm(
        tmp_path,
        owner="async",
        trace_enabled=True,
        growth_per_iteration=1024 * 1024,
    )
    growing = evaluate_telemetry_admission(
        layout=benchmark_layout(tmp_path), run_id=RUN_ID, memory_run_id=MEMORY_RUN_ID
    )
    assert gates_by_name(growing)["memory-growth"]["status"] == "fail"

    memory_artifact_path(
        benchmark_layout(tmp_path),
        run_id=MEMORY_RUN_ID,
        owner="async",
        trace_enabled=True,
    ).unlink()
    missing = evaluate_telemetry_admission(
        layout=benchmark_layout(tmp_path), run_id=RUN_ID, memory_run_id=MEMORY_RUN_ID
    )
    assert gates_by_name(missing)["memory-growth"]["status"] == "insufficient_evidence"


def test_admission_keeps_missing_sql_memory_and_pool_evidence_insufficient(
    tmp_path: Path,
) -> None:
    for owner in ("sync", "async"):
        for pair_index in range(20):
            write_pair_evidence(tmp_path, owner=owner, pair_index=pair_index)

    result = evaluate_telemetry_admission(
        layout=benchmark_layout(tmp_path),
        run_id=RUN_ID,
    )

    gates = gates_by_name(result)
    assert gates["publication-state-parity"]["status"] == "pass"
    assert gates["terminal-trace-accounting"]["status"] == "pass"
    assert gates["stage-counter-reconciliation"]["status"] == "pass"
    assert gates["cold-p95-overhead"]["status"] == "pass"
    assert gates["sql-write-parity"]["status"] == "insufficient_evidence"
    assert gates["memory-growth"]["status"] == "insufficient_evidence"
    assert gates["pooled-connection-state"]["status"] == "insufficient_evidence"
    assert result["status"] == "insufficient_evidence"
    artifact = (
        benchmark_layout(tmp_path).report_directory(RUN_ID) / "telemetry-admission.json"
    ).read_text(encoding="utf-8")
    assert "enabled-sync-0" not in artifact
    assert json.loads(artifact)["exploratory_pair_minimum_per_owner"] == 20


def test_admission_fails_when_cold_p95_overhead_exceeds_limit(
    tmp_path: Path,
) -> None:
    for owner in ("sync", "async"):
        for pair_index in range(20):
            write_pair_evidence(
                tmp_path,
                owner=owner,
                pair_index=pair_index,
                enabled_duration_ms=200.0 if owner == "sync" else 102.0,
            )

    result = evaluate_telemetry_admission(
        layout=benchmark_layout(tmp_path),
        run_id=RUN_ID,
    )

    assert result["status"] == "fail"
    overhead = gates_by_name(result)["cold-p95-overhead"]
    assert overhead["status"] == "fail"
    sync_groups = {
        key: value
        for key, value in overhead["details"]["groups"].items()
        if key.startswith("sync/production/baseline/")
    }
    assert len(sync_groups) == 1
    assert next(iter(sync_groups.values()))["allowed_overhead_ms"] == 50.0


def test_admission_fails_on_missing_terminal_event_and_missing_pairs(
    tmp_path: Path,
) -> None:
    write_pair_evidence(
        tmp_path,
        owner="sync",
        pair_index=0,
        write_trace=False,
    )

    result = evaluate_telemetry_admission(
        layout=benchmark_layout(tmp_path),
        run_id=RUN_ID,
    )

    gates = gates_by_name(result)
    assert gates["terminal-trace-accounting"]["status"] == "fail"
    assert gates["cold-p95-overhead"]["status"] == "insufficient_evidence"
    assert result["status"] == "fail"


def test_admission_checks_symmetric_sql_counts_and_terminal_outcome(
    tmp_path: Path,
) -> None:
    write_pair_evidence(
        tmp_path,
        owner="sync",
        pair_index=0,
        trace_outcome="failure",
        sql_counts=(10, 4),
        enabled_sql_counts=(11, 4),
    )

    gates = gates_by_name(
        evaluate_telemetry_admission(layout=benchmark_layout(tmp_path), run_id=RUN_ID)
    )

    assert gates["terminal-trace-accounting"]["status"] == "fail"
    assert (
        gates["terminal-trace-accounting"]["details"]["unsuccessful_committed_events"]
        == 1
    )
    assert gates["sql-write-parity"]["status"] == "fail"
    assert gates["sql-write-parity"]["details"]["mismatched_pairs"] == 1


def test_admission_does_not_pool_different_code_commits(tmp_path: Path) -> None:
    for owner in ("sync", "async"):
        for pair_index in range(20):
            write_pair_evidence(
                tmp_path,
                owner=owner,
                pair_index=pair_index,
                code_commit="first" if pair_index < 10 else "second",
                sql_counts=(10, 4),
            )

    gates = gates_by_name(
        evaluate_telemetry_admission(layout=benchmark_layout(tmp_path), run_id=RUN_ID)
    )

    assert gates["sql-write-parity"]["status"] == "pass"
    overhead = gates["cold-p95-overhead"]
    assert overhead["status"] == "insufficient_evidence"
    assert len(overhead["details"]["groups"]) == 4
    assert all(
        group["pair_count"] == 10 for group in overhead["details"]["groups"].values()
    )


def test_admission_rejects_mismatched_or_missing_source_identity(
    tmp_path: Path,
) -> None:
    write_pair_evidence(
        tmp_path,
        owner="sync",
        pair_index=0,
        enabled_source_content_digest="sha256:" + "d" * 64,
    )
    layout = benchmark_layout(tmp_path)
    mismatched = gates_by_name(
        evaluate_telemetry_admission(layout=layout, run_id=RUN_ID)
    )
    assert mismatched["publication-state-parity"]["status"] == "fail"

    records_path = layout.publication_record_path(RUN_ID)
    records = cli_support.read_jsonl(records_path)
    for record in records:
        record.pop("source_content_digest")
    records_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    missing = gates_by_name(evaluate_telemetry_admission(layout=layout, run_id=RUN_ID))
    assert missing["publication-state-parity"]["status"] == "fail"


def test_admission_does_not_pool_different_source_contents(tmp_path: Path) -> None:
    for owner in ("sync", "async"):
        for pair_index in range(20):
            write_pair_evidence(
                tmp_path,
                owner=owner,
                pair_index=pair_index,
                source_content_digest=(
                    SOURCE_CONTENT_DIGEST if pair_index < 10 else "sha256:" + "d" * 64
                ),
                sql_counts=(10, 4),
            )

    gates = gates_by_name(
        evaluate_telemetry_admission(layout=benchmark_layout(tmp_path), run_id=RUN_ID)
    )
    overhead = gates["cold-p95-overhead"]
    assert overhead["status"] == "insufficient_evidence"
    assert len(overhead["details"]["groups"]) == 4
    assert all(
        group["pair_count"] == 10 for group in overhead["details"]["groups"].values()
    )


def test_admission_rechecks_saved_state_after_parity_artifact(tmp_path: Path) -> None:
    write_pair_evidence(tmp_path, owner="sync", pair_index=0)
    layout = benchmark_layout(tmp_path)
    snapshot_path = (
        layout.clone_directory(RUN_ID)
        / "samples"
        / "enabled-sync-0"
        / "state-after.json"
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["relation_counts"]["document_chunks"] = 3
    cli_support.write_json(snapshot_path, snapshot)

    gates = gates_by_name(evaluate_telemetry_admission(layout=layout, run_id=RUN_ID))

    assert gates["publication-state-parity"]["status"] == "fail"
    assert gates["publication-state-parity"]["details"]["invalid_pair_artifacts"] == 1
