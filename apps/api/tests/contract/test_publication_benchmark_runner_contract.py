"""Contract tests for the publication benchmark runner and executions."""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)
from tests.support.publication_benchmark_support import (
    benchmark_layout,
    ensure_benchmark_import_path,
    expectations_for,
    write_listed_clone,
    write_synthetic_frozen_input,
)

ensure_benchmark_import_path()

from scripts.publication_benchmark.frozen_input import (  # noqa: E402
    FrozenInputError,
)
from scripts.publication_benchmark.clone_state import read_clone_record  # noqa: E402
from scripts.publication_benchmark.guards import BenchmarkGuardError  # noqa: E402
from scripts.publication_benchmark.publication_execution import (  # noqa: E402
    PublicationExecutionRequest,
    PublicationExecutionResult,
    build_publication_environment,
    database_url_for_owner,
)
from scripts.publication_benchmark.run_publication import (  # noqa: E402
    run_id_for_database_url_file,
    run_publication_point,
)
from scripts.publication_benchmark.run_record import (  # noqa: E402
    RunRecordError,
    resolve_source_content_digest,
)

RUN_ID = "runner-contract-run"
DATABASE_URL = "postgresql+psycopg2://postgres:secret@127.0.0.1:55450/db"
SOURCE_CONTENT_DIGEST = "sha256:" + "c" * 64


@pytest.fixture(autouse=True)
def freeze_test_source_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic layouts isolate runner behavior from the checkout's Git state."""
    monkeypatch.setattr(
        "scripts.publication_benchmark.run_publication.resolve_source_content_digest",
        lambda repository_root: SOURCE_CONTENT_DIGEST,
    )


@dataclass
class FakeExecutor:
    """Executor stub that records the request it received."""

    result: PublicationExecutionResult
    requests: list[PublicationExecutionRequest]

    def execute(
        self,
        request: PublicationExecutionRequest,
    ) -> PublicationExecutionResult:
        self.requests.append(request)
        return self.result


@dataclass
class TracingFakeExecutor(FakeExecutor):
    """Return a terminal payload bound to the supplied opaque attempt ref."""

    def execute(
        self,
        request: PublicationExecutionRequest,
    ) -> PublicationExecutionResult:
        self.requests.append(request)
        return replace(
            self.result,
            terminal_trace={
                "attempt_ref": request.publication_attempt_ref,
                "owner": request.owner,
                "outcome": "success",
                "duration_ms": 1_234.5,
            },
        )


def build_fake_result() -> PublicationExecutionResult:
    return PublicationExecutionResult(
        outcome="committed",
        duration_ms=1_234.5,
        state_before={
            "scope_ref": "scope-fake",
            "relation_counts": {"document_chunks": 0},
        },
        state_after={
            "scope_ref": "scope-fake",
            "relation_counts": {"document_chunks": 4},
        },
        counts={"submitted_chunks": 4, "persisted_document_chunks": 4},
        sql_observation={
            "statement_count": 12,
            "write_statement_count": 5,
            "write_row_count": 10,
            "write_row_count_complete": True,
        },
        effects={"applied": False, "cache_invalidation_key_refs": []},
        stage_durations={},
        background_activity={
            "before": {"autovacuum_workers": 0},
            "after": {"autovacuum_workers": 0},
        },
    )


def contract_sync_database_url() -> str:
    """Return the contract database URL for the synchronous publication owner."""
    url = make_url(get_contract_database_url()).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def prepare_contract_database(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())


def test_run_publication_refuses_a_missing_frozen_input(tmp_path: Path) -> None:
    layout = benchmark_layout(tmp_path)
    write_listed_clone(layout, run_id=RUN_ID, database_url=DATABASE_URL)

    with pytest.raises(FrozenInputError, match="manifest not found"):
        run_publication_point(
            layout=layout,
            owner="sync",
            database_url=DATABASE_URL,
            input_directory=layout.frozen_input_directory(),
            strategy="baseline",
            mode="cold",
            run_id=RUN_ID,
        )


def test_run_publication_refuses_an_unlisted_clone(tmp_path: Path) -> None:
    layout = benchmark_layout(tmp_path)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())

    with pytest.raises(BenchmarkGuardError, match="unlisted clone"):
        run_publication_point(
            layout=layout,
            owner="sync",
            database_url=DATABASE_URL,
            input_directory=layout.frozen_input_directory(),
            strategy="baseline",
            mode="cold",
            run_id=RUN_ID,
            expectations=expectations_for(payload),
        )


def test_run_publication_refuses_a_strategy_that_differs_from_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import sys

    layout = benchmark_layout(tmp_path)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())
    write_listed_clone(layout, run_id=RUN_ID, database_url=DATABASE_URL)
    live_config = importlib.import_module("shared.core.config")
    monkeypatch.setattr(
        live_config.settings,
        "KNOWHERE_PUBLICATION_STRATEGY",
        "candidate",
    )
    monkeypatch.setitem(sys.modules, "shared.core.config", live_config)

    with pytest.raises(BenchmarkGuardError, match="does not match"):
        run_publication_point(
            layout=layout,
            owner="sync",
            database_url=DATABASE_URL,
            input_directory=layout.frozen_input_directory(),
            strategy="baseline",
            mode="cold",
            run_id=RUN_ID,
            expectations=expectations_for(payload),
        )


def test_run_publication_refuses_a_cold_sample_on_a_non_fresh_clone(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())
    write_listed_clone(
        layout,
        run_id=RUN_ID,
        database_url=DATABASE_URL,
        state="destroyed",
    )

    with pytest.raises(BenchmarkGuardError, match="not listed as usable"):
        run_publication_point(
            layout=layout,
            owner="sync",
            database_url=DATABASE_URL,
            input_directory=layout.frozen_input_directory(),
            strategy="baseline",
            mode="cold",
            run_id=RUN_ID,
            expectations=expectations_for(payload),
        )


def test_run_id_defaults_to_the_listed_clone_directory(tmp_path: Path) -> None:
    layout = benchmark_layout(tmp_path)
    database_url_file = layout.database_url_path(RUN_ID)
    database_url_file.parent.mkdir(parents=True, exist_ok=True)
    database_url_file.write_text("postgresql://localhost/db\n", encoding="utf-8")

    assert (
        run_id_for_database_url_file(
            layout=layout,
            database_url_file=database_url_file,
            run_id=None,
        )
        == RUN_ID
    )


def test_run_id_rejects_a_database_url_file_outside_the_listed_clone(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    database_url_file = tmp_path / "database-url"
    database_url_file.write_text("postgresql://localhost/db\n", encoding="utf-8")

    with pytest.raises(BenchmarkGuardError, match="listed clone"):
        run_id_for_database_url_file(
            layout=layout,
            database_url_file=database_url_file,
            run_id=RUN_ID,
        )


def test_run_publication_records_a_sample_with_the_owner_executor(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    payload, manifest = write_synthetic_frozen_input(layout.frozen_input_directory())
    write_listed_clone(layout, run_id=RUN_ID, database_url=DATABASE_URL)
    executor = FakeExecutor(result=build_fake_result(), requests=[])

    result = run_publication_point(
        layout=layout,
        owner="sync",
        database_url=DATABASE_URL,
        input_directory=layout.frozen_input_directory(),
        strategy="baseline",
        mode="cold",
        run_id=RUN_ID,
        expectations=expectations_for(payload),
        executor=executor,
    )

    assert len(executor.requests) == 1
    request = executor.requests[0]
    assert request.owner == "sync"
    assert request.mode == "cold"
    assert request.trace_enabled is False
    assert len(request.chunks) == len(payload["chunks"])
    assert result["record"]["outcome"] == "committed"
    assert result["record"]["publication_duration_ms"] == pytest.approx(1234.5)
    assert result["record"]["input_digest"] == manifest.digest
    assert result["record"]["strategy"] == "baseline"
    assert result["record"]["source_content_digest"] == SOURCE_CONTENT_DIGEST
    assert result["record"]["publication_attempt_ref"].startswith("attempt-")
    assert read_clone_record(layout.clone_record_path(RUN_ID)).state == "sampled"

    records = [
        json.loads(line)
        for line in layout.publication_record_path(RUN_ID)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["counts"]["persisted_document_chunks"] == 4
    assert records[0]["sql_observation"]["statement_count"] == 12
    assert records[0]["sql_observation"]["write_statement_count"] == 5

    state_after = json.loads(
        layout.state_after_path(RUN_ID).read_text(encoding="utf-8")
    )
    assert state_after["relation_counts"]["document_chunks"] == 4
    assert payload["chunks"][0]["content"] not in json.dumps(state_after)
    assert not (layout.clone_directory(RUN_ID) / "publication-traces.jsonl").exists()


def test_run_publication_rejects_source_drift_without_writing_a_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = benchmark_layout(tmp_path)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())
    write_listed_clone(layout, run_id=RUN_ID, database_url=DATABASE_URL)
    observations = iter((SOURCE_CONTENT_DIGEST, "sha256:" + "d" * 64))
    monkeypatch.setattr(
        "scripts.publication_benchmark.run_publication.resolve_source_content_digest",
        lambda repository_root: next(observations),
    )

    with pytest.raises(RunRecordError, match="source changed during publication"):
        run_publication_point(
            layout=layout,
            owner="sync",
            database_url=DATABASE_URL,
            input_directory=layout.frozen_input_directory(),
            strategy="baseline",
            mode="cold",
            run_id=RUN_ID,
            expectations=expectations_for(payload),
            executor=FakeExecutor(result=build_fake_result(), requests=[]),
        )

    assert read_clone_record(layout.clone_record_path(RUN_ID)).state == "sampled"
    assert not layout.publication_record_path(RUN_ID).exists()


def test_source_digest_includes_untracked_files_and_ignores_benchmark_artifacts(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".benchmarks/\n", encoding="utf-8")
    (tmp_path / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "--", ".gitignore", "tracked.py"],
        cwd=tmp_path,
        check=True,
    )
    baseline = resolve_source_content_digest(tmp_path)

    artifact = tmp_path / ".benchmarks" / "sample.json"
    artifact.parent.mkdir()
    artifact.write_text("{}", encoding="utf-8")
    assert resolve_source_content_digest(tmp_path) == baseline

    (tmp_path / "untracked.py").write_text("VALUE = 2\n", encoding="utf-8")
    with_untracked = resolve_source_content_digest(tmp_path)
    assert with_untracked != baseline

    (tmp_path / "tracked.py").write_text("VALUE = 3\n", encoding="utf-8")
    assert resolve_source_content_digest(tmp_path) != with_untracked


def test_run_publication_uses_database_bounded_job_and_result_ids(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())
    long_run_id = "trace-pair-sync-20260923-a"
    write_listed_clone(layout, run_id=long_run_id, database_url=DATABASE_URL)
    executor = FakeExecutor(result=build_fake_result(), requests=[])

    run_publication_point(
        layout=layout,
        owner="sync",
        database_url=DATABASE_URL,
        input_directory=layout.frozen_input_directory(),
        strategy="baseline",
        mode="cold",
        run_id=long_run_id,
        expectations=expectations_for(payload),
        executor=executor,
    )

    scope = executor.requests[0].scope
    assert len(scope.job_ref) <= 36
    assert len(scope.revision_ref) <= 36
    assert scope.job_ref != scope.revision_ref


def test_run_publication_explicit_replacement_uses_existing_document_and_new_refs(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())
    write_listed_clone(layout, run_id=RUN_ID, database_url=DATABASE_URL)
    executor = FakeExecutor(result=build_fake_result(), requests=[])

    run_publication_point(
        layout=layout,
        owner="sync",
        database_url=DATABASE_URL,
        input_directory=layout.frozen_input_directory(),
        strategy="baseline",
        mode="warm",
        run_id=RUN_ID,
        replacement_document_id="doc_existing_123",
        expectations=expectations_for(payload),
        executor=executor,
    )

    scope = executor.requests[0].scope
    assert scope.document_id_ref == "doc_existing_123"
    assert scope.job_ref.startswith("bench-rjob-")
    assert scope.revision_ref.startswith("bench-rres-")
    assert len(scope.job_ref) <= 36
    assert len(scope.revision_ref) <= 36


def test_run_publication_persists_trace_for_enabled_sample(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())
    write_listed_clone(layout, run_id=RUN_ID, database_url=DATABASE_URL)
    executor = TracingFakeExecutor(result=build_fake_result(), requests=[])

    result = run_publication_point(
        layout=layout,
        owner="sync",
        database_url=DATABASE_URL,
        input_directory=layout.frozen_input_directory(),
        strategy="baseline",
        mode="cold",
        run_id=RUN_ID,
        expectations=expectations_for(payload),
        executor=executor,
        trace_enabled=True,
    )

    assert executor.requests[0].trace_enabled is True
    attempt_ref = result["record"]["publication_attempt_ref"]
    assert executor.requests[0].publication_attempt_ref == attempt_ref
    trace_file = layout.clone_directory(RUN_ID) / "publication-traces.jsonl"
    traces = [json.loads(line) for line in trace_file.read_text().splitlines()]
    assert traces == [
        {
            "attempt_ref": attempt_ref,
            "owner": "sync",
            "outcome": "success",
            "duration_ms": 1_234.5,
        }
    ]
    accounting = json.loads(
        (layout.clone_directory(RUN_ID) / "trace-accounting.json").read_text()
    )
    assert accounting["terminal_event_attempt_refs"] == [attempt_ref]


def test_run_publication_accepts_a_freshly_reset_clone_for_cold_samples(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())
    write_listed_clone(
        layout,
        run_id=RUN_ID,
        database_url=DATABASE_URL,
        state="reset",
    )
    executor = FakeExecutor(result=build_fake_result(), requests=[])

    result = run_publication_point(
        layout=layout,
        owner="async",
        database_url=DATABASE_URL,
        input_directory=layout.frozen_input_directory(),
        strategy="baseline",
        mode="cold",
        run_id=RUN_ID,
        expectations=expectations_for(payload),
        executor=executor,
    )

    assert result["record"]["outcome"] == "committed"
    assert executor.requests[0].mode == "cold"
    assert executor.requests[0].owner == "async"


def test_run_publication_refuses_a_clone_already_used_for_a_cold_sample(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())
    write_listed_clone(
        layout,
        run_id=RUN_ID,
        database_url=DATABASE_URL,
        state="sampled",
    )

    with pytest.raises(BenchmarkGuardError, match="freshly created clone"):
        run_publication_point(
            layout=layout,
            owner="sync",
            database_url=DATABASE_URL,
            input_directory=layout.frozen_input_directory(),
            strategy="baseline",
            mode="cold",
            run_id=RUN_ID,
            expectations=expectations_for(payload),
        )


@pytest.mark.parametrize("trace_enabled", [False, True])
def test_run_publication_executes_the_sync_owner_against_a_clone(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
    tmp_path: Path,
    trace_enabled: bool,
) -> None:
    prepare_contract_database(monkeypatch, postgresql_proc)
    database_url = contract_sync_database_url()
    layout = benchmark_layout(tmp_path)
    payload, manifest = write_synthetic_frozen_input(layout.frozen_input_directory())
    write_listed_clone(layout, run_id=RUN_ID, database_url=database_url)

    result = run_publication_point(
        layout=layout,
        owner="sync",
        database_url=database_url,
        input_directory=layout.frozen_input_directory(),
        strategy="baseline",
        mode="cold",
        run_id=RUN_ID,
        expectations=expectations_for(payload),
        trace_enabled=trace_enabled,
    )

    assert result["record"]["outcome"] == "committed"
    assert result["record"]["input_digest"] == manifest.digest
    assert result["counts"]["persisted_document_chunks"] == len(payload["chunks"])
    state_after = json.loads(
        layout.state_after_path(RUN_ID).read_text(encoding="utf-8")
    )
    counts = state_after["relation_counts"]
    assert counts["documents_active"] == 1
    assert counts["document_sections"] >= 1
    assert counts["document_chunks"] == len(payload["chunks"])
    assert counts["document_map_units"] >= 1
    assert counts["document_map_unit_tokens"] >= 1
    assert state_after["namespace_snapshot"] is not None
    assert result["effects"]["applied"] is False
    record = json.loads(
        layout.publication_record_path(RUN_ID).read_text().splitlines()[0]
    )
    assert bool(record["stage_durations"]) is trace_enabled
    if trace_enabled:
        traces = [
            json.loads(line)
            for line in (layout.clone_directory(RUN_ID) / "publication-traces.jsonl")
            .read_text()
            .splitlines()
        ]
        assert traces[0]["attempt_ref"] == result["record"]["publication_attempt_ref"]
        assert traces[0]["sql"]["statement_count"] > 0
        assert traces[0]["stages"]["commit"]["duration_ms"] >= 0


@pytest.mark.parametrize("trace_enabled", [False, True])
def test_run_publication_executes_the_async_owner_against_a_clone(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
    tmp_path: Path,
    trace_enabled: bool,
) -> None:
    prepare_contract_database(monkeypatch, postgresql_proc)
    database_url = get_contract_database_url()
    layout = benchmark_layout(tmp_path)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())
    write_listed_clone(layout, run_id=RUN_ID, database_url=database_url)

    result = run_publication_point(
        layout=layout,
        owner="async",
        database_url=database_url,
        input_directory=layout.frozen_input_directory(),
        strategy="baseline",
        mode="cold",
        run_id=RUN_ID,
        expectations=expectations_for(payload),
        trace_enabled=trace_enabled,
    )

    assert result["record"]["outcome"] == "committed"
    state_after = json.loads(
        layout.state_after_path(RUN_ID).read_text(encoding="utf-8")
    )
    counts = state_after["relation_counts"]
    assert counts["documents_active"] == 1
    assert counts["document_chunks"] == len(payload["chunks"])
    assert counts["document_map_unit_tokens"] >= 1
    assert state_after["namespace_snapshot"] is not None
    record = json.loads(
        layout.publication_record_path(RUN_ID).read_text().splitlines()[0]
    )
    assert bool(record["stage_durations"]) is trace_enabled
    if trace_enabled:
        traces = [
            json.loads(line)
            for line in (layout.clone_directory(RUN_ID) / "publication-traces.jsonl")
            .read_text()
            .splitlines()
        ]
        assert traces[0]["attempt_ref"] == result["record"]["publication_attempt_ref"]
        assert traces[0]["sql"]["statement_count"] > 0
        assert traces[0]["stages"]["commit"]["duration_ms"] >= 0


def test_run_publication_rejects_shell_alternation_style_inputs() -> None:
    """The runner takes one matrix point per invocation, never a shell list."""
    from scripts.publication_benchmark.run_publication import (
        build_argument_parser,
    )

    parser = build_argument_parser()
    arguments = parser.parse_args(
        [
            "--owner",
            "sync",
            "--db-url-file",
            "clone/database-url",
            "--input",
            "inputs/spacex-s1-production",
            "--strategy",
            "baseline",
            "--mode",
            "cold",
        ]
    )
    assert arguments.strategy == "baseline"
    assert arguments.mode == "cold"
    assert arguments.trace == "disabled"
    enabled = parser.parse_args(
        [
            "--owner",
            "sync",
            "--db-url-file",
            "clone/database-url",
            "--input",
            "inputs/spacex-s1-production",
            "--strategy",
            "baseline",
            "--mode",
            "cold",
            "--trace",
            "enabled",
        ]
    )
    assert enabled.trace == "enabled"
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--owner",
                "sync",
                "--db-url-file",
                "clone/database-url",
                "--input",
                "inputs/spacex-s1-production",
                "--strategy",
                "baseline|candidate",
                "--mode",
                "cold",
            ]
        )


def test_publication_environment_uses_only_benchmark_placeholders() -> None:
    environment = build_publication_environment(
        database_url="postgresql+psycopg2://postgres:secret@127.0.0.1:55450/db",
        run_id="env-contract",
        strategy="candidate",
    )

    assert environment["DATABASE_URL"] == (
        "postgresql+psycopg2://postgres:secret@127.0.0.1:55450/db"
    )
    assert environment["KNOWHERE_PUBLICATION_STRATEGY"] == "candidate"
    assert environment["S3_BUCKET_NAME"] == "knowhere-publication-benchmark"
    assert environment["DS_URL"].endswith(".invalid/v1")
    assert environment["TELEMETRY_ENABLED"] == "false"
    assert environment["LOGFIRE_TOKEN"] == ""
    for value in environment.values():
        assert "amazonaws.com" not in value
        assert "aliyuncs.com" not in value


def test_database_url_for_owner_switches_the_driver() -> None:
    base = "postgresql+psycopg2://postgres:secret@127.0.0.1:55450/db"

    assert database_url_for_owner(base, owner="sync").startswith(
        "postgresql+psycopg2://"
    )
    assert database_url_for_owner(base, owner="async").startswith(
        "postgresql+asyncpg://"
    )
    with pytest.raises(Exception, match="unsupported publication owner"):
        database_url_for_owner(base, owner="threaded")


def test_run_publication_applies_benchmark_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = benchmark_layout(tmp_path)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())
    write_listed_clone(layout, run_id=RUN_ID, database_url=DATABASE_URL)
    executor = FakeExecutor(result=build_fake_result(), requests=[])
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://prod:secret@db/db")

    run_publication_point(
        layout=layout,
        owner="async",
        database_url=DATABASE_URL,
        input_directory=layout.frozen_input_directory(),
        strategy="baseline",
        mode="cold",
        run_id=RUN_ID,
        expectations=expectations_for(payload),
        executor=executor,
    )

    import os

    assert os.environ["DATABASE_URL"] == database_url_for_owner(
        DATABASE_URL,
        owner="async",
    )
    assert executor.requests[0].database_url.startswith("postgresql+asyncpg://")
