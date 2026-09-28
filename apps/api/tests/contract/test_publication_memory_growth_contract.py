"""Contracts for bounded, redacted publication memory evidence."""

# ruff: noqa: E402

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.publication_benchmark_support import (
    benchmark_layout,
    ensure_benchmark_import_path,
    write_listed_clone,
    write_synthetic_frozen_input,
)

ensure_benchmark_import_path()

from scripts.publication_benchmark.check_memory_growth import (  # noqa: E402
    MEASURED_COUNT,
    MemoryGrowthError,
    SCHEMA_VERSION,
    WARMUP_COUNT,
    artifact_path,
    collect_memory_series,
    evaluate_memory_series,
    validate_test_clone,
)
from scripts.publication_benchmark.clone_state import (  # noqa: E402
    write_clone_record,
)
from scripts.publication_benchmark.guards import BenchmarkGuardError  # noqa: E402
from scripts.publication_benchmark.publication_execution import (  # noqa: E402
    PublicationExecutionRequest,
    PublicationExecutionResult,
)

RUN_ID = "memory-small-test"
DATABASE_URL = (
    "postgresql+psycopg2://postgres:secret@127.0.0.1:55450/knowhere_benchmark"
)


def write_small_test_clone(tmp_path: Path) -> tuple[Path, Path]:
    layout = benchmark_layout(tmp_path)
    record = write_listed_clone(layout, run_id=RUN_ID, database_url=DATABASE_URL)
    safe_source = replace(
        record.source,
        volume_name="knowhere-memory-test-source",
        container_name="knowhere-memory-test-pg",
    )
    write_clone_record(
        layout.clone_record_path(RUN_ID), replace(record, source=safe_source)
    )
    return layout.database_url_path(RUN_ID), layout.clone_record_path(RUN_ID)


def make_series(values: list[int], *, trace_enabled: bool = True) -> dict[str, object]:
    observations: list[dict[str, object]] = []
    for iteration, rss in enumerate([values[0]] * WARMUP_COUNT + values):
        observations.append(
            {
                "iteration": iteration,
                "phase": "warmup" if iteration < WARMUP_COUNT else "measured",
                "outcome": "committed",
                "persisted_chunks": 4 * (iteration + 1),
                "persisted_map_units": 2 * (iteration + 1),
                "submitted_chunks": 4,
                "sql_statement_count": 1,
                "has_publication_stages": trace_enabled,
                "terminal_count": int(trace_enabled),
                "terminal_matches_attempt": trace_enabled,
                "terminal_chunk_count": 4 if trace_enabled else 0,
                "database_size_after_bytes": 1024,
                "increased_persisted_state": True,
                "rss_after_gc_bytes": rss,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "trace_enabled": trace_enabled,
        "input_chunk_count": 4,
        "initial_database_bytes": 1024,
        "observations": observations,
    }


def test_memory_gate_distinguishes_plateau_growth_and_disagreement() -> None:
    base = 300 * 1024 * 1024
    stable = make_series([base + (index % 3) * 1024 for index in range(MEASURED_COUNT)])
    growing = make_series(
        [base + index * 1024 * 1024 for index in range(MEASURED_COUNT)]
    )
    late_step = make_series(
        [base] * (MEASURED_COUNT - 15) + [base + 40 * 1024 * 1024] * 15
    )

    assert evaluate_memory_series(stable)["status"] == "pass"
    assert evaluate_memory_series(growing)["status"] == "fail"
    assert evaluate_memory_series(late_step)["status"] == "insufficient_evidence"
    assert (
        evaluate_memory_series(make_series([base] * 59))["status"]
        == "insufficient_evidence"
    )


def test_memory_collector_refuses_september_and_oversized_targets(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    path = layout.database_url_path(RUN_ID)
    record = write_listed_clone(layout, run_id=RUN_ID, database_url=DATABASE_URL)

    with pytest.raises(BenchmarkGuardError, match="separate small test source"):
        validate_test_clone(layout=layout, run_id=RUN_ID, database_url_path=path)

    safe_source = replace(
        record.source,
        volume_name="knowhere-memory-test-source",
        container_name="knowhere-memory-test-pg",
    )
    too_large = replace(
        record,
        source=safe_source,
        database_identity={**record.database_identity, "size_bytes": 600 * 1024 * 1024},
    )
    write_clone_record(layout.clone_record_path(RUN_ID), too_large)
    with pytest.raises(BenchmarkGuardError, match="small test database"):
        validate_test_clone(layout=layout, run_id=RUN_ID, database_url_path=path)

    write_clone_record(
        layout.clone_record_path(RUN_ID), replace(record, source=safe_source)
    )
    with pytest.raises(BenchmarkGuardError, match="listed clone URL file"):
        validate_test_clone(
            layout=layout,
            run_id=RUN_ID,
            database_url_path=tmp_path / "other-database-url",
        )


def test_memory_collector_uses_one_process_and_writes_only_scalar_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url_path, _ = write_small_test_clone(tmp_path)
    layout = benchmark_layout(tmp_path)
    input_directory = layout.frozen_input_directory("memory-input")
    write_synthetic_frozen_input(input_directory)
    requests: list[PublicationExecutionRequest] = []

    def execute(request: PublicationExecutionRequest) -> PublicationExecutionResult:
        requests.append(request)
        return PublicationExecutionResult(
            outcome="committed",
            duration_ms=1.0,
            state_before={},
            state_after={},
            counts={
                "submitted_chunks": 4,
                "persisted_document_chunks": 4 * len(requests),
                "persisted_map_units": 2 * len(requests),
            },
            effects={},
            stage_durations={"chunks_prepare": 0.1, "commit": 0.1},
            sql_observation={"statement_count": 1},
            terminal_trace={
                "attempt_ref": request.publication_attempt_ref,
                "outcome": "success",
                "counts": {"chunks": 4},
            },
        )

    monkeypatch.setattr(
        "scripts.publication_benchmark.publication_execution.apply_publication_environment",
        lambda **kwargs: {},
    )
    monkeypatch.setattr(
        "scripts.publication_benchmark.check_memory_growth.resolve_source_content_digest",
        lambda repository_root: "sha256:" + "a" * 64,
    )
    result = collect_memory_series(
        layout=layout,
        run_id=RUN_ID,
        database_url_path=database_url_path,
        input_directory=input_directory,
        owner="sync",
        trace_enabled=True,
        strategy="baseline",
        execute=execute,
        read_rss=lambda: 300 * 1024 * 1024,
        verify_database=lambda database_url, record: 1024,
    )

    assert len(requests) == WARMUP_COUNT + MEASURED_COUNT
    assert len({request.scope.job_ref for request in requests}) == len(requests)
    assert len({request.scope.revision_ref for request in requests}) == len(requests)
    assert len({request.scope.source_file_name for request in requests}) == len(requests)
    assert len({request.scope.namespace_ref for request in requests}) == 1
    assert len({request.publication_attempt_ref for request in requests}) == len(
        requests
    )
    assert result["source_content_digest"] == "sha256:" + "a" * 64
    gate = result["gate"]
    assert isinstance(gate, dict)
    assert gate["status"] == "pass"
    path = artifact_path(layout, run_id=RUN_ID, owner="sync", trace_enabled=True)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved["observations"]) == WARMUP_COUNT + MEASURED_COUNT
    assert "secret" not in path.read_text(encoding="utf-8")
    assert "database_url" not in path.read_text(encoding="utf-8")


def test_memory_collector_stops_before_another_write_when_database_grows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url_path, _ = write_small_test_clone(tmp_path)
    layout = benchmark_layout(tmp_path)
    input_directory = layout.frozen_input_directory("memory-input")
    write_synthetic_frozen_input(input_directory)
    path = artifact_path(layout, run_id=RUN_ID, owner="sync", trace_enabled=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"gate":{"status":"pass"}}', encoding="utf-8")
    calls = 0

    def execute(request: PublicationExecutionRequest) -> PublicationExecutionResult:
        nonlocal calls
        calls += 1
        return PublicationExecutionResult(
            outcome="committed",
            duration_ms=1.0,
            state_before={},
            state_after={},
            counts={
                "submitted_chunks": 4,
                "persisted_document_chunks": 4,
                "persisted_map_units": 2,
            },
            effects={},
            stage_durations={},
            sql_observation={"statement_count": 1},
        )

    size_checks = iter([1024, 385 * 1024 * 1024])
    monkeypatch.setattr(
        "scripts.publication_benchmark.publication_execution.apply_publication_environment",
        lambda **kwargs: {},
    )
    monkeypatch.setattr(
        "scripts.publication_benchmark.check_memory_growth.resolve_source_content_digest",
        lambda repository_root: "sha256:" + "a" * 64,
    )
    with pytest.raises(BenchmarkGuardError, match="growth limit"):
        collect_memory_series(
            layout=layout,
            run_id=RUN_ID,
            database_url_path=database_url_path,
            input_directory=input_directory,
            owner="sync",
            trace_enabled=False,
            strategy="baseline",
            execute=execute,
            read_rss=lambda: 300 * 1024 * 1024,
            verify_database=lambda database_url, record: next(size_checks),
        )

    assert calls == 1
    assert not path.exists()


def test_memory_collector_discards_stale_evidence_if_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url_path, _ = write_small_test_clone(tmp_path)
    layout = benchmark_layout(tmp_path)
    input_directory = layout.frozen_input_directory("memory-input")
    write_synthetic_frozen_input(input_directory)
    path = artifact_path(layout, run_id=RUN_ID, owner="sync", trace_enabled=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"gate":{"status":"pass"}}', encoding="utf-8")
    digests = iter(("sha256:" + "a" * 64, "sha256:" + "b" * 64))
    monkeypatch.setattr(
        "scripts.publication_benchmark.check_memory_growth.resolve_source_content_digest",
        lambda repository_root: next(digests),
    )
    monkeypatch.setattr(
        "scripts.publication_benchmark.publication_execution.apply_publication_environment",
        lambda **kwargs: {},
    )
    calls = 0

    def execute(request: PublicationExecutionRequest) -> PublicationExecutionResult:
        nonlocal calls
        calls += 1
        return PublicationExecutionResult(
            outcome="committed",
            duration_ms=1.0,
            state_before={},
            state_after={},
            counts={
                "submitted_chunks": 4,
                "persisted_document_chunks": 4 * calls,
                "persisted_map_units": 2 * calls,
            },
            effects={},
            stage_durations={},
            sql_observation={"statement_count": 1},
        )

    with pytest.raises(MemoryGrowthError, match="source changed"):
        collect_memory_series(
            layout=layout,
            run_id=RUN_ID,
            database_url_path=database_url_path,
            input_directory=input_directory,
            owner="sync",
            trace_enabled=False,
            strategy="baseline",
            execute=execute,
            read_rss=lambda: 300 * 1024 * 1024,
            verify_database=lambda database_url, record: 1024,
        )

    assert calls == WARMUP_COUNT + MEASURED_COUNT
    assert not path.exists()
