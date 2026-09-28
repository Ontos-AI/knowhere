"""Contract tests for benchmark report statistics, gates, and redaction."""

# ruff: noqa: E402

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.publication_benchmark_support import (
    benchmark_layout,
    copy_plan_document,
    ensure_benchmark_import_path,
    expectations_for,
    write_listed_clone,
    write_synthetic_frozen_input,
)

ensure_benchmark_import_path()

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.aggregate import (  # noqa: E402
    aggregate_run,
    build_argument_parser,
)
from scripts.publication_benchmark.compare_state import (  # noqa: E402
    StateComparisonError,
    compare_run_samples,
    state_parity_artifact_path,
)
from scripts.publication_benchmark.state_parity import SEMANTIC_COMPONENTS  # noqa: E402
from scripts.publication_benchmark.state_snapshot import (  # noqa: E402
    SEMANTIC_STATE_SCHEMA_VERSION,
    STATE_SNAPSHOT_SCHEMA_VERSION,
)
from scripts.publication_benchmark.report import (  # noqa: E402
    DURATION_THRESHOLD_SECONDS,
    ReportContractError,
    assert_report_is_redacted,
    duration_statistics,
    evaluate_duration_gate,
    evaluate_paired_improvement,
    percentile,
    write_report_artifacts,
)
from scripts.publication_benchmark.run_record import (  # noqa: E402
    PublicationRunRecord,
    assert_runs_are_comparable,
    find_comparability_exceptions,
    find_comparability_conflicts,
)
from scripts.publication_benchmark.trace_accounting import (  # noqa: E402
    TraceAccountingError,
    account_terminal_traces,
    assert_terminal_trace_accounting,
)

RUN_ID = "report-contract-run"
DATABASE_URL = "postgresql+psycopg2://postgres:secret@127.0.0.1:55450/db"


def build_run_record(
    *,
    sample_id: str,
    owner: str = "sync",
    strategy: str = "baseline",
    mode: str = "cold",
    duration_ms: float = 5_000.0,
    profile: str = "production",
    outcome: str = "committed",
) -> PublicationRunRecord:
    return PublicationRunRecord(
        run_id=RUN_ID,
        sample_id=sample_id,
        strategy=strategy,
        owner=owner,
        mode=mode,
        code_commit="caba7b6d6",
        dependency_lock_digest="sha256:lock",
        postgres_profile=profile,
        postgres_version="15.17",
        postgres_settings_digest="sha256:settings",
        redis_namespace="publication-benchmark",
        input_digest="sha256:frozen-input",
        clone_id=RUN_ID,
        clone_source_digest="sha256:source-volume-digest",
        host_id="host-contract",
        started_at="2026-09-23T00:00:00Z",
        ended_at="2026-09-23T00:00:01Z",
        outcome=outcome,
        publication_duration_ms=duration_ms,
        publication_attempt_ref=f"attempt-{sample_id}",
    )


def test_state_parity_command_compares_saved_sample_artifacts(tmp_path: Path) -> None:
    layout = benchmark_layout(tmp_path)
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
    for sample_id, trace_enabled in (("disabled-1", False), ("enabled-1", True)):
        cli_support.append_jsonl(
            layout.publication_record_path(RUN_ID),
            {
                **build_run_record(sample_id=sample_id).to_dict(),
                "input_digest": "sha256:" + "a" * 64,
                "trace_enabled": trace_enabled,
            },
        )
        cli_support.write_json(
            layout.clone_directory(RUN_ID) / "samples" / sample_id / "state-after.json",
            snapshot,
        )

    result = compare_run_samples(
        layout=layout,
        run_id=RUN_ID,
        disabled_sample_id="disabled-1",
        enabled_sample_id="enabled-1",
    )

    assert result.status == "pass"
    artifact = json.loads(
        state_parity_artifact_path(
            layout=layout,
            run_id=RUN_ID,
            disabled_sample_id="disabled-1",
            enabled_sample_id="enabled-1",
        ).read_text()
    )
    assert artifact["status"] == "pass"
    assert artifact["sample_refs"] != ["disabled-1", "enabled-1"]


def test_state_parity_preserves_each_pair_and_rejects_incomplete_record(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    snapshot = {
        "schema_version": STATE_SNAPSHOT_SCHEMA_VERSION,
        "scope_ref": "scope-1234567890abcdef",
        "source_file_name_refs": ["ref-1234567890abcdef"],
        "relation_counts": {"document_chunks": 1},
        "chunk_type_counts": {"text": 1},
        "namespace_generation": 1,
        "semantic_fingerprints": {
            "schema_version": SEMANTIC_STATE_SCHEMA_VERSION,
            **{component: "sha256:" + "b" * 64 for component in SEMANTIC_COMPONENTS},
        },
    }
    for sample_id, trace_enabled in (
        ("disabled-1", False),
        ("enabled-1", True),
        ("enabled-2", True),
    ):
        cli_support.append_jsonl(
            layout.publication_record_path(RUN_ID),
            {
                **build_run_record(sample_id=sample_id).to_dict(),
                "input_digest": "sha256:" + "a" * 64,
                "trace_enabled": trace_enabled,
            },
        )
        cli_support.write_json(
            layout.clone_directory(RUN_ID) / "samples" / sample_id / "state-after.json",
            snapshot,
        )

    compare_run_samples(
        layout=layout,
        run_id=RUN_ID,
        disabled_sample_id="disabled-1",
        enabled_sample_id="enabled-1",
    )
    compare_run_samples(
        layout=layout,
        run_id=RUN_ID,
        disabled_sample_id="disabled-1",
        enabled_sample_id="enabled-2",
    )

    pair_files = sorted((layout.report_directory(RUN_ID) / "parity").glob("*.json"))
    assert len(pair_files) == 2
    assert all(json.loads(path.read_text())["status"] == "pass" for path in pair_files)

    incomplete = {
        "sample_id": "enabled-3",
        "trace_enabled": True,
        "outcome": "committed",
    }
    cli_support.append_jsonl(layout.publication_record_path(RUN_ID), incomplete)
    with pytest.raises(StateComparisonError, match="incomplete"):
        compare_run_samples(
            layout=layout,
            run_id=RUN_ID,
            disabled_sample_id="disabled-1",
            enabled_sample_id="enabled-3",
        )


def test_percentile_and_duration_statistics() -> None:
    samples = [1.0, 2.0, 3.0, 4.0, 5.0]

    assert percentile(samples, 0.5) == 3.0
    assert percentile(samples, 0.95) == pytest.approx(4.8)
    statistics = duration_statistics(samples)
    assert statistics["count"] == 5
    assert statistics["min_ms"] == 1.0
    assert statistics["max_ms"] == 5.0
    assert duration_statistics([])["count"] == 0


def test_duration_gate_requires_the_frozen_sample_count() -> None:
    gate = evaluate_duration_gate([4.0] * 20)
    assert gate.status == "insufficient_samples"
    assert gate.required_samples == 59

    gate = evaluate_duration_gate([4.0] * 59)
    assert gate.status == "pass"
    assert gate.lower_confidence_bound is not None
    assert gate.lower_confidence_bound >= 0.95

    gate = evaluate_duration_gate([4.0] * 58 + [DURATION_THRESHOLD_SECONDS])
    assert gate.status == "fail"
    assert gate.failures == 1


def test_aggregate_cli_does_not_allow_changing_the_frozen_sample_count() -> None:
    parser = build_argument_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--report-dir", "reports/sample", "--required-samples", "1"])


def test_paired_bootstrap_reports_resolved_and_unresolved_results() -> None:
    resolved = evaluate_paired_improvement(
        [10_000.0] * 20,
        [8_000.0] * 20,
        resamples=200,
    )
    assert resolved.resolved
    assert resolved.mean_improvement_ms == pytest.approx(2_000.0)
    assert resolved.ci_low_ms > 0.0

    unresolved = evaluate_paired_improvement(
        [10_000.0, 9_000.0, 11_000.0, 10_500.0],
        [10_100.0, 8_900.0, 11_200.0, 10_400.0],
        resamples=200,
    )
    assert not unresolved.resolved
    assert unresolved.ci_low_ms <= 0.0 <= unresolved.ci_high_ms


def test_paired_bootstrap_requires_paired_samples() -> None:
    with pytest.raises(ReportContractError, match="same number"):
        evaluate_paired_improvement([1.0, 2.0], [1.0])


def test_report_redaction_rejects_prohibited_keys_and_values() -> None:
    with pytest.raises(ReportContractError, match="prohibited keys"):
        assert_report_is_redacted({"scope": {"user_id": "someone"}})

    with pytest.raises(ReportContractError, match="prohibited production values"):
        assert_report_is_redacted(
            {"notes": ["spacex-s1.pdf"]},
            prohibited_values=("spacex-s1.pdf",),
        )

    assert_report_is_redacted(
        {"notes": ["frozen input manifest digest: sha256:abc"]},
        prohibited_values=("spacex-s1.pdf",),
    )


def test_write_report_artifacts_writes_four_checked_files(tmp_path: Path) -> None:
    summary = {
        "schema_version": "publication-benchmark-summary/1",
        "run_id": RUN_ID,
        "generated_at": "2026-09-23T00:00:00Z",
        "sample_count": 1,
        "statistics": {},
        "duration_gate": None,
        "terminal_trace_accounting": None,
        "records": [],
        "notes": [],
    }

    write_report_artifacts(
        report_directory=tmp_path,
        summary=summary,
        resource={"schema_version": "resource", "samples": []},
        gate_results={"schema_version": "gates", "gates": []},
    )

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "gate-results.json",
        "resource.json",
        "summary.json",
        "summary.md",
    ]
    with pytest.raises(ReportContractError):
        write_report_artifacts(
            report_directory=tmp_path,
            summary={**summary, "notes": ["spacex-s1.pdf"]},
            resource={},
            gate_results={},
            prohibited_values=("spacex-s1.pdf",),
        )


def test_terminal_trace_accounting_contract() -> None:
    accounting = account_terminal_traces(
        publication_attempt_refs=["attempt-1", "attempt-2"],
        terminal_event_attempt_refs=["attempt-1", "attempt-2"],
    )
    assert accounting.is_accounted
    assert accounting.coverage_ratio == 1.0
    assert_terminal_trace_accounting(accounting)

    missing = account_terminal_traces(
        publication_attempt_refs=["attempt-1", "attempt-2"],
        terminal_event_attempt_refs=["attempt-1"],
    )
    assert not missing.is_accounted
    assert missing.missing_attempt_refs == ("attempt-2",)
    with pytest.raises(TraceAccountingError, match="accounting failed"):
        assert_terminal_trace_accounting(missing)

    duplicated = account_terminal_traces(
        publication_attempt_refs=["attempt-1"],
        terminal_event_attempt_refs=["attempt-1", "attempt-1"],
    )
    assert duplicated.duplicate_attempt_refs == ("attempt-1",)

    unexpected = account_terminal_traces(
        publication_attempt_refs=["attempt-1"],
        terminal_event_attempt_refs=["attempt-1", "attempt-9"],
    )
    assert unexpected.unexpected_attempt_refs == ("attempt-9",)

    with pytest.raises(TraceAccountingError, match="unique"):
        account_terminal_traces(
            publication_attempt_refs=["attempt-1", "attempt-1"],
            terminal_event_attempt_refs=[],
        )


@pytest.mark.parametrize(
    ("terminal_refs", "expected_status"),
    [
        (["attempt-sample-1"], "pass"),
        ([], "fail"),
        (["attempt-sample-1", "attempt-sample-1"], "fail"),
    ],
)
def test_aggregate_checks_terminal_events_when_trace_file_exists(
    tmp_path: Path,
    terminal_refs: list[str],
    expected_status: str,
) -> None:
    layout = benchmark_layout(tmp_path)
    copy_plan_document(tmp_path)
    write_listed_clone(layout, run_id=RUN_ID, database_url=DATABASE_URL)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())
    cli_support.append_jsonl(
        layout.publication_record_path(RUN_ID),
        {
            **build_run_record(sample_id="sample-1").to_dict(),
            "trace_enabled": True,
        },
    )
    cli_support.append_jsonl(
        layout.publication_record_path(RUN_ID),
        {
            **build_run_record(sample_id="sample-disabled", owner="async").to_dict(),
            "trace_enabled": False,
        },
    )
    (layout.clone_directory(RUN_ID) / "trace-accounting.json").write_text(
        json.dumps({"terminal_event_attempt_refs": terminal_refs}),
        encoding="utf-8",
    )

    aggregate_run(
        layout=layout,
        run_id=RUN_ID,
        expectations=expectations_for(payload),
    )

    summary = json.loads(
        (layout.report_directory(RUN_ID) / "summary.json").read_text(encoding="utf-8")
    )
    assert (
        summary["trace_mode_statistics"]["sync/production/baseline/cold/enabled"][
            "count"
        ]
        == 1
    )
    assert (
        summary["trace_mode_statistics"]["async/production/baseline/cold/disabled"][
            "count"
        ]
        == 1
    )
    assert summary["duration_trace_mode"] == "enabled"
    assert "cold/async/production/baseline" not in summary["duration_gates"]
    gate_document = json.loads(
        (layout.report_directory(RUN_ID) / "gate-results.json").read_text(
            encoding="utf-8"
        )
    )
    gates = {gate["gate"]: gate for gate in gate_document["gates"]}
    trace_gate = gates["terminal-trace-accounting"]
    assert trace_gate["status"] == expected_status
    assert trace_gate["details"]["publication_attempts"] == 1
    assert trace_gate["details"]["terminal_events"] == len(terminal_refs)


def test_aggregate_fails_accounting_for_traced_sample_without_terminal_file(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    copy_plan_document(tmp_path)
    write_listed_clone(layout, run_id=RUN_ID, database_url=DATABASE_URL)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())
    cli_support.append_jsonl(
        layout.publication_record_path(RUN_ID),
        {
            **build_run_record(sample_id="sample-1").to_dict(),
            "trace_enabled": True,
        },
    )

    aggregate_run(
        layout=layout,
        run_id=RUN_ID,
        expectations=expectations_for(payload),
    )

    gate_document = json.loads(
        (layout.report_directory(RUN_ID) / "gate-results.json").read_text(
            encoding="utf-8"
        )
    )
    gates = {gate["gate"]: gate for gate in gate_document["gates"]}
    trace_gate = gates["terminal-trace-accounting"]
    assert trace_gate["status"] == "fail"
    assert trace_gate["details"]["missing_attempt_refs"] == ["attempt-sample-1"]


def test_aggregate_requires_committed_baseline_samples_for_phase_zero(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    copy_plan_document(tmp_path)
    write_listed_clone(layout, run_id=RUN_ID, database_url=DATABASE_URL)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())
    for record in (
        build_run_record(sample_id="failed-sync", outcome="failed"),
        build_run_record(
            sample_id="candidate-async", owner="async", strategy="candidate"
        ),
    ):
        cli_support.append_jsonl(
            layout.publication_record_path(RUN_ID),
            record.to_dict(),
        )

    aggregate_run(
        layout=layout,
        run_id=RUN_ID,
        expectations=expectations_for(payload),
    )

    gate_document = json.loads(
        (layout.report_directory(RUN_ID) / "gate-results.json").read_text()
    )
    gates = {gate["gate"]: gate for gate in gate_document["gates"]}
    assert gates["publication-samples"]["status"] == "pass"
    assert gates["publication-samples"]["details"]["committed"] == 1
    assert gates["no-op-publication-per-owner"]["status"] == "fail"
    assert gates["no-op-publication-per-owner"]["details"][
        "committed_baseline_owners"
    ] == []


def test_aggregate_does_not_count_missing_duration_as_zero_seconds(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    copy_plan_document(tmp_path)
    write_listed_clone(layout, run_id=RUN_ID, database_url=DATABASE_URL)
    payload, _ = write_synthetic_frozen_input(layout.frozen_input_directory())
    for index in range(59):
        record = replace(
            build_run_record(sample_id=f"missing-duration-{index}"),
            publication_duration_ms=None,
        )
        cli_support.append_jsonl(
            layout.publication_record_path(RUN_ID),
            record.to_dict(),
        )

    aggregate_run(
        layout=layout,
        run_id=RUN_ID,
        expectations=expectations_for(payload),
    )

    summary = json.loads(
        (layout.report_directory(RUN_ID) / "summary.json").read_text()
    )
    gate = summary["duration_gates"]["cold/sync/production/baseline"]
    assert gate["status"] == "insufficient_samples"
    assert gate["sample_count"] == 0


def test_run_record_comparability_ignores_strategy_and_run_identity() -> None:
    baseline = build_run_record(sample_id="sample-baseline")
    candidate = build_run_record(sample_id="sample-candidate", strategy="candidate")

    assert find_comparability_conflicts(baseline, candidate) == ()
    assert_runs_are_comparable(baseline, candidate)

    drifted = build_run_record(
        sample_id="sample-drifted",
        strategy="candidate",
        profile="conservative",
    )
    assert find_comparability_conflicts(baseline, drifted) == ("postgres_profile",)
    with pytest.raises(Exception, match="not comparable"):
        assert_runs_are_comparable(baseline, drifted)

    templated_baseline = replace(baseline, template_content_digest="sha256:" + "1" * 64)
    templated_candidate = replace(
        candidate, template_content_digest="sha256:" + "2" * 64
    )
    assert find_comparability_conflicts(templated_baseline, templated_candidate) == (
        "template_content_digest",
    )

    same_strategy = build_run_record(sample_id="sample-other")
    with pytest.raises(Exception, match="different strategies"):
        assert_runs_are_comparable(baseline, same_strategy)


def test_run_record_allows_only_declared_isolated_redis_namespaces() -> None:
    baseline = replace(
        build_run_record(sample_id="sample-baseline"),
        run_id="sample-baseline-run",
        redis_namespace="publication-benchmark:sample-baseline-run",
    )
    candidate = replace(
        build_run_record(sample_id="sample-candidate", strategy="candidate"),
        run_id="sample-candidate-run",
        redis_namespace="publication-benchmark:sample-candidate-run",
    )

    assert find_comparability_exceptions(baseline, candidate) == ("redis_namespace",)
    assert find_comparability_conflicts(baseline, candidate) == ()
    assert_runs_are_comparable(baseline, candidate)

    malformed = replace(candidate, redis_namespace="publication-benchmark:shared")
    assert find_comparability_exceptions(baseline, malformed) == ()
    assert find_comparability_conflicts(baseline, malformed) == ("redis_namespace",)


def test_aggregate_run_writes_reports_without_production_data(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    copy_plan_document(tmp_path)
    write_listed_clone(layout, run_id=RUN_ID, database_url=DATABASE_URL)
    payload, manifest = write_synthetic_frozen_input(layout.frozen_input_directory())
    records = [
        build_run_record(sample_id="sample-1", owner="sync"),
        build_run_record(sample_id="sample-2", owner="async"),
        build_run_record(
            sample_id="sample-3",
            owner="async",
            strategy="candidate",
            duration_ms=3_000.0,
        ),
    ]
    for record in records:
        cli_support.append_jsonl(
            layout.publication_record_path(RUN_ID),
            record.to_dict(),
        )
    cli_support.append_jsonl(
        layout.publication_record_path(RUN_ID),
        {
            **build_run_record(sample_id="sample-4").to_dict(),
            "background_activity": {
                "before": {"autovacuum_workers": 2},
                "after": {"autovacuum_workers": 0},
            },
        },
    )

    result = aggregate_run(
        layout=layout,
        run_id=RUN_ID,
        generated_at="2026-09-23T00:00:00Z",
        expectations=expectations_for(payload),
    )

    report_directory = layout.report_directory(RUN_ID)
    assert sorted(path.name for path in report_directory.iterdir()) == [
        "gate-results.json",
        "resource.json",
        "summary.json",
        "summary.md",
    ]
    summary = json.loads((report_directory / "summary.json").read_text("utf-8"))
    gate_document = json.loads(
        (report_directory / "gate-results.json").read_text("utf-8")
    )
    gates = {gate["gate"]: gate for gate in gate_document["gates"]}

    assert summary["sample_count"] == 4
    assert summary["duration_gates"]["cold/sync/production/baseline"]["status"] == (
        "insufficient_samples"
    )
    assert gates["frozen-input-digest"]["status"] == "pass"
    assert gates["clone-lifecycle"]["status"] == "pass"
    assert gates["no-op-publication-per-owner"]["status"] == "pass"
    assert gates["verification-cases"]["status"] == "pass"
    assert gates["verification-cases"]["details"]["implemented_case_ids"] == [
        "ID-001",
        "ID-006",
        "ST-001",
        "ST-002",
    ]
    assert "ID-001" not in gates["verification-cases"]["details"][
        "placeholder_case_ids"
    ]
    assert gates["terminal-trace-accounting"]["status"] == "pass"
    terminal_trace_gate = gates["terminal-trace-accounting"]
    assert terminal_trace_gate["details"]["mode"] == "contract-only"
    assert terminal_trace_gate["details"]["contract"]["inputs"] == [
        "publication_attempt_refs",
        "terminal_event_attempt_refs",
    ]
    assert terminal_trace_gate["details"]["contract"]["outputs"] == [
        "publication_attempts",
        "terminal_events",
        "missing_attempt_refs",
        "duplicate_attempt_refs",
        "unexpected_attempt_refs",
        "is_accounted",
        "coverage_ratio",
    ]
    assert gates["duration"]["status"] == "insufficient_samples"
    assert gates["redaction"]["status"] == "pass"
    assert result["status"] == "ok"
    assert any("overlapped an autovacuum worker" in note for note in summary["notes"])
    serialized = json.dumps(summary, sort_keys=True)
    for prohibited in (manifest.digest,):
        assert prohibited in serialized
    for prohibited in (
        payload["chunks"][0]["content"],
        payload["chunks"][0]["path"],
    ):
        assert prohibited not in serialized
