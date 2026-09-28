"""Aggregate publication samples into the Phase 0 report artifacts.

    uv run python scripts/publication_benchmark/aggregate.py \
      --report-dir .benchmarks/publication/reports/<run-id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.cases import (  # noqa: E402
    CASE_REGISTRY,
    IMPLEMENTED_CASE_IDS,
    PLAN_DOCUMENT_RELATIVE_PATH,
    find_registry_mismatches,
)
from scripts.publication_benchmark.clone_state import (  # noqa: E402
    CloneRecordError,
    read_clone_record,
)
from scripts.publication_benchmark.frozen_input import (  # noqa: E402
    SPACEX_S1_PRODUCTION_EXPECTATIONS,
    FrozenInputError,
    FrozenInputExpectations,
    get_prohibited_payload_values,
    read_frozen_input,
)
from scripts.publication_benchmark.layout import (  # noqa: E402
    BenchmarkLayout,
    validate_run_id,
)
from scripts.publication_benchmark.report import (  # noqa: E402
    REQUIRED_COLD_SAMPLES,
    DurationGateResult,
    ResourceSample,
    build_gate_results_document,
    build_resource_document,
    build_run_summary,
    duration_statistics,
    evaluate_duration_gate,
    write_report_artifacts,
)
from scripts.publication_benchmark.run_record import (  # noqa: E402
    PublicationRunRecord,
)
from scripts.publication_benchmark.trace_accounting import (  # noqa: E402
    account_terminal_traces,
    terminal_trace_accounting_contract,
)

TRACE_ACCOUNTING_FILE_NAME: str = "trace-accounting.json"
RESOURCE_RECORD_FILE_NAME: str = "resource.jsonl"
MEASUREMENT_NOTES_FILE_NAME: str = "measurement-notes.json"


def aggregate_run(
    *,
    layout: BenchmarkLayout,
    run_id: str,
    generated_at: str | None = None,
    expectations: FrozenInputExpectations = SPACEX_S1_PRODUCTION_EXPECTATIONS,
) -> dict[str, Any]:
    """Build and write every report artifact for one run identifier."""
    resolved_run_id = validate_run_id(run_id)
    generated = generated_at or cli_support.utc_now_iso()
    clone_directory = layout.clone_directory(resolved_run_id)
    raw_records = cli_support.read_jsonl(
        layout.publication_record_path(resolved_run_id)
    )
    records = [PublicationRunRecord.from_dict(raw) for raw in raw_records]
    payload, manifest, input_error = _load_frozen_input(
        layout,
        expectations=expectations,
    )
    prohibited_values = (
        get_prohibited_payload_values(payload) if payload is not None else ()
    )
    resource_samples = _load_resource_samples(clone_directory)
    terminal_trace_accounting = _load_terminal_trace_accounting(
        clone_directory,
        raw_records=raw_records,
    )
    duration_gates = _build_duration_gates(
        records,
        raw_records=raw_records,
        required_samples=REQUIRED_COLD_SAMPLES,
    )
    gates = _build_gate_results(
        layout=layout,
        run_id=resolved_run_id,
        clone_directory=clone_directory,
        records=records,
        input_error=input_error,
        terminal_trace_accounting=terminal_trace_accounting,
        duration_gates=duration_gates,
    )
    summary = build_run_summary(
        run_id=resolved_run_id,
        generated_at=generated,
        records=records,
        terminal_trace_accounting=terminal_trace_accounting,
        duration_gate=duration_gates.get("primary"),
        notes=(
            "Phase 0 records wall-clock publication duration; per-stage "
            "telemetry arrives in Phase 1.",
            "Post-commit effects are recorded as intents only; no benchmark "
            "run enqueues webhooks or writes a production Redis namespace.",
            _background_activity_note(raw_records),
            *_load_measurement_notes(clone_directory),
            (
                f"frozen input manifest digest: {manifest.digest}"
                if manifest is not None
                else "frozen input manifest unavailable"
            ),
        ),
    )
    summary["duration_gates"] = {
        key: value.to_dict() for key, value in duration_gates.items()
    }
    summary["trace_mode_statistics"] = _build_trace_mode_statistics(raw_records)
    summary["duration_trace_mode"] = (
        "enabled"
        if any(raw.get("trace_enabled") is True for raw in raw_records)
        else "legacy_untraced"
    )
    resource = build_resource_document(
        run_id=resolved_run_id,
        generated_at=generated,
        samples=resource_samples,
        notes=(
            "WAL, temp bytes, RSS, CPU, and lock-wait sampling land with the "
            "Phase 5 resource envelope.",
        ),
    )
    gate_results = build_gate_results_document(
        run_id=resolved_run_id,
        generated_at=generated,
        gates=gates,
    )
    write_report_artifacts(
        report_directory=layout.ensure_report_directory(resolved_run_id),
        summary=summary,
        resource=resource,
        gate_results=gate_results,
        prohibited_values=prohibited_values,
    )
    return {
        "command": "aggregate",
        "status": "ok",
        "report_directory": str(layout.report_directory(resolved_run_id)),
        "sample_count": len(records),
        "gates": gates,
    }


def _load_frozen_input(
    layout: BenchmarkLayout,
    *,
    expectations: FrozenInputExpectations = SPACEX_S1_PRODUCTION_EXPECTATIONS,
) -> tuple[dict[str, Any] | None, Any | None, str | None]:
    input_directory = layout.frozen_input_directory()
    try:
        payload, manifest = read_frozen_input(
            input_directory,
            expectations=expectations,
        )
    except FrozenInputError as error:
        return None, None, str(error)
    return payload, manifest, None


def _background_activity_note(raw_records: Sequence[Mapping[str, Any]]) -> str:
    """Summarize samples that raced database maintenance work."""
    if not any("background_activity" in raw for raw in raw_records):
        return (
            "background activity was not recorded for these samples; the "
            "harness now captures autovacuum workers around every sample"
        )
    contended = [
        str(raw.get("sample_id")) for raw in raw_records if _autovacuum_workers(raw) > 0
    ]
    if not contended:
        return (
            "no sample observed a running autovacuum worker; clones are settled "
            "with VACUUM (ANALYZE) before they are marked ready"
        )
    return (
        f"{len(contended)} sample(s) overlapped an autovacuum worker "
        f"({', '.join(contended)}); treat those durations as contended evidence"
    )


def _load_measurement_notes(clone_directory: Path) -> tuple[str, ...]:
    """Read operator-recorded measurement caveats for one run."""
    path = clone_directory / MEASUREMENT_NOTES_FILE_NAME
    if not path.is_file():
        return ()
    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return (f"measurement notes file is not a list: {path.name}",)
    return tuple(str(note) for note in raw)


def _autovacuum_workers(raw: Mapping[str, Any]) -> int:
    activity = raw.get("background_activity")
    if not isinstance(activity, Mapping):
        return 0
    workers = 0
    for phase in ("before", "after"):
        snapshot = activity.get(phase)
        if isinstance(snapshot, Mapping):
            workers = max(workers, int(snapshot.get("autovacuum_workers") or 0))
    return workers


def _load_resource_samples(clone_directory: Path) -> list[ResourceSample]:
    samples: list[ResourceSample] = []
    for index, raw in enumerate(
        cli_support.read_jsonl(clone_directory / RESOURCE_RECORD_FILE_NAME)
    ):
        values = {key: value for key, value in raw.items() if key not in ("sample_id",)}
        samples.append(
            ResourceSample(
                sample_id=str(raw.get("sample_id") or f"resource-{index}"),
                values=values,
            )
        )
    return samples


def _load_terminal_trace_accounting(
    clone_directory: Path,
    *,
    raw_records: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    traced_attempt_refs = [
        str(raw["publication_attempt_ref"])
        for raw in raw_records
        if raw.get("trace_enabled") is True
    ]
    path = clone_directory / TRACE_ACCOUNTING_FILE_NAME
    if not path.is_file() and not traced_attempt_refs:
        return None
    import json

    raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    terminal_refs = [str(ref) for ref in raw.get("terminal_event_attempt_refs", [])]
    accounting = account_terminal_traces(
        publication_attempt_refs=traced_attempt_refs,
        terminal_event_attempt_refs=terminal_refs,
    )
    return accounting.to_dict()


def _build_trace_mode_statistics(
    raw_records: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Keep enabled and disabled publication durations separate for admission."""
    statistics: dict[str, Mapping[str, Any]] = {}
    keys = sorted(
        {
            (
                str(raw.get("owner")),
                str(raw.get("postgres_profile")),
                str(raw.get("strategy")),
                str(raw.get("mode")),
                bool(raw.get("trace_enabled")),
            )
            for raw in raw_records
            if raw.get("publication_duration_ms") is not None
        }
    )
    for owner, profile, strategy, mode, trace_enabled in keys:
        durations = [
            float(raw["publication_duration_ms"])
            for raw in raw_records
            if raw.get("owner") == owner
            and raw.get("postgres_profile") == profile
            and raw.get("strategy") == strategy
            and raw.get("mode") == mode
            and bool(raw.get("trace_enabled")) == trace_enabled
            and raw.get("publication_duration_ms") is not None
        ]
        trace_mode = "enabled" if trace_enabled else "disabled"
        statistics[f"{owner}/{profile}/{strategy}/{mode}/{trace_mode}"] = (
            duration_statistics(durations)
        )
    return statistics


def _build_duration_gates(
    records: Sequence[PublicationRunRecord],
    *,
    raw_records: Sequence[Mapping[str, Any]],
    required_samples: int,
) -> dict[str, DurationGateResult]:
    traced_sample_ids = {
        str(raw["sample_id"]) for raw in raw_records if raw.get("trace_enabled") is True
    }
    eligible_records = [
        record
        for record in records
        if not traced_sample_ids or record.sample_id in traced_sample_ids
    ]
    groups: dict[str, DurationGateResult] = {}
    keys = sorted(
        {
            (record.owner, record.postgres_profile, record.strategy)
            for record in eligible_records
            if record.mode == "cold"
        }
    )
    for owner, profile, strategy in keys:
        durations = [
            record.publication_duration_ms / 1000.0
            for record in eligible_records
            if record.mode == "cold"
            and record.owner == owner
            and record.postgres_profile == profile
            and record.strategy == strategy
            and record.outcome == "committed"
            and record.publication_duration_ms is not None
        ]
        groups[f"cold/{owner}/{profile}/{strategy}"] = evaluate_duration_gate(
            durations,
            required_samples=required_samples,
        )
    primary_key = next(
        (key for key in groups if key.startswith("cold/sync/")),
        next(iter(groups), None),
    )
    if primary_key is not None:
        groups["primary"] = groups[primary_key]
    return groups


def _build_gate_results(
    *,
    layout: BenchmarkLayout,
    run_id: str,
    clone_directory: Path,
    records: Sequence[PublicationRunRecord],
    input_error: str | None,
    terminal_trace_accounting: Mapping[str, Any] | None,
    duration_gates: Mapping[str, DurationGateResult],
) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []
    gates.append(
        {
            "gate": "frozen-input-digest",
            "status": "fail" if input_error else "pass",
            "details": input_error or "frozen input manifest and digest verified",
        }
    )
    try:
        clone_record = read_clone_record(layout.clone_record_path(run_id))
        clone_status = "pass"
        clone_details: Any = {
            "state": clone_record.state,
            "profile": clone_record.profile,
            "schema_revision": clone_record.schema_revision,
            "clone_source_digest": clone_record.source.digest,
        }
    except CloneRecordError as error:
        clone_status = "fail"
        clone_details = str(error)
    gates.append(
        {
            "gate": "clone-lifecycle",
            "status": clone_status,
            "details": clone_details,
        }
    )
    owners = sorted({record.owner for record in records})
    committed_records = [record for record in records if record.outcome == "committed"]
    committed_baseline_owners = sorted(
        {record.owner for record in committed_records if record.strategy == "baseline"}
    )
    gates.append(
        {
            "gate": "publication-samples",
            "status": "pass" if committed_records else "fail",
            "details": {
                "sample_count": len(records),
                "owners": owners,
                "committed": len(committed_records),
            },
        }
    )
    gates.append(
        {
            "gate": "no-op-publication-per-owner",
            "status": (
                "pass"
                if {"sync", "async"}.issubset(set(committed_baseline_owners))
                else "fail"
            ),
            "details": {
                "committed_baseline_owners": committed_baseline_owners,
                "required_owners": ["sync", "async"],
            },
        }
    )
    plan_path = layout.repository_root / PLAN_DOCUMENT_RELATIVE_PATH
    mismatches = find_registry_mismatches(plan_path)
    implemented_case_ids = sorted(
        case_id for case_id in IMPLEMENTED_CASE_IDS if case_id in CASE_REGISTRY
    )
    placeholder_case_ids = sorted(
        case_id for case_id in CASE_REGISTRY if case_id not in IMPLEMENTED_CASE_IDS
    )
    gates.append(
        {
            "gate": "verification-cases",
            # Registry drift is a gate failure. Implemented cases are reported
            # separately so they are not counted as placeholders.
            "status": "pass" if not mismatches else "fail",
            "details": {
                "registered": len(CASE_REGISTRY),
                "registry_mismatches": list(mismatches),
                "implemented_case_ids": implemented_case_ids,
                "placeholder_case_ids": placeholder_case_ids,
                "placeholders_fail_loudly": True,
            },
        }
    )
    gates.append(
        {
            "gate": "terminal-trace-accounting",
            # Phase 0 freezes the accounting boundary; Phase 1 supplies the
            # terminal events that will be checked against it. A report with
            # no trace file therefore passes this contract-only gate while a
            # present trace file is still reconciled strictly.
            "status": (
                "pass"
                if terminal_trace_accounting is None
                else (
                    "pass" if terminal_trace_accounting.get("is_accounted") else "fail"
                )
            ),
            "details": (
                {
                    "mode": "contract-only",
                    "contract": terminal_trace_accounting_contract(),
                    "measurement": "terminal events are emitted in Phase 1",
                }
                if terminal_trace_accounting is None
                else terminal_trace_accounting
            ),
        }
    )
    gates.append(
        {
            "gate": "duration",
            "status": (
                duration_gates["primary"].status
                if "primary" in duration_gates
                else "insufficient_samples"
            ),
            "details": {key: value.to_dict() for key, value in duration_gates.items()},
        }
    )
    gates.append(
        {
            "gate": "redaction",
            "status": "pass",
            "details": "report artifacts were redaction-checked before writing",
        }
    )
    return gates


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the aggregate command line parser."""
    parser = argparse.ArgumentParser(
        description="Aggregate Publication benchmark samples into reports",
    )
    parser.add_argument(
        "--report-dir",
        required=True,
        type=Path,
        help="Report directory, for example .benchmarks/publication/reports/<run-id>",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the aggregate command."""
    arguments = build_argument_parser().parse_args(argv)
    layout = BenchmarkLayout.from_path()
    run_id = arguments.report_dir.resolve().name
    try:
        result = aggregate_run(
            layout=layout,
            run_id=run_id,
        )
    except (ValueError, KeyError) as error:
        cli_support.print_result(
            {
                "command": "aggregate",
                "status": "failed",
                "error": str(error),
            }
        )
        return cli_support.EXIT_GATE_FAILED
    cli_support.print_result(result)
    return cli_support.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
