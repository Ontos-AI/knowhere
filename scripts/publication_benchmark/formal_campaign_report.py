"""Aggregate a split formal publication campaign without exposing sample data.

Formal campaigns use one disposable clone per sample, so the ordinary
single-clone aggregate command cannot produce a campaign-level admission
report.  This module reads only redacted publication records and trace
accounting files, then emits opaque identities, statistics, and explicit
evidence gaps.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.clone_state import read_clone_record  # noqa: E402
from scripts.publication_benchmark.layout import BenchmarkLayout  # noqa: E402
from scripts.publication_benchmark.report import (  # noqa: E402
    assert_report_is_redacted,
    duration_statistics,
    evaluate_duration_gate,
    evaluate_paired_improvement,
)
from scripts.publication_benchmark.run_record import (  # noqa: E402
    PublicationRunRecord,
    resolve_source_content_digest,
)
from scripts.publication_benchmark.trace_accounting import (  # noqa: E402
    account_terminal_traces,
)

FORMAL_CAMPAIGN_SCHEMA_VERSION: str = "publication-formal-campaign/1"
FORMAL_SAMPLE_COUNT: int = 59
FORMAL_OWNERS: tuple[str, ...] = ("sync", "async")
FORMAL_STRATEGIES: tuple[str, ...] = ("baseline", "candidate")
_RUN_PATTERN = re.compile(
    r"^formal59-(?:compact|current)-(?P<round>[0-9]{2})-(?P<owner>sync|async)-"
    r"(?P<strategy>baseline|candidate)$"
)
_IDENTITY_FIELDS: tuple[str, ...] = (
    "code_commit",
    "dependency_lock_digest",
    "source_content_digest",
    "template_content_digest",
    "input_digest",
    "clone_source_digest",
    "host_id",
    "postgres_settings_digest",
    "postgres_version",
    "postgres_profile",
)
_EXPECTED_COUNTS: Mapping[str, int] = {
    "persisted_document_chunks": 922,
    "persisted_map_units": 830,
    "persisted_token_rows": 88551,
    "submitted_chunks": 922,
    "submitted_image_chunks": 99,
    "submitted_table_chunks": 153,
    "submitted_text_chunks": 670,
}


class FormalCampaignError(RuntimeError):
    """Raised when formal campaign artifacts violate their input contract."""


def _read_records(
    clones_root: Path,
) -> list[tuple[int, PublicationRunRecord, Mapping[str, Any], Path]]:
    rows: list[tuple[int, PublicationRunRecord, Mapping[str, Any], Path]] = []
    for directory in sorted(clones_root.glob("formal59-*")):
        match = _RUN_PATTERN.fullmatch(directory.name)
        if match is None:
            continue
        records_path = directory / "publication.jsonl"
        if not records_path.is_file():
            raise FormalCampaignError(
                f"publication record is missing for {directory.name}"
            )
        raw_records = cli_support.read_jsonl(records_path)
        if len(raw_records) != 1:
            raise FormalCampaignError(
                f"expected one publication record for {directory.name}"
            )
        raw = raw_records[0]
        try:
            record = PublicationRunRecord.from_dict(raw)
        except (KeyError, TypeError, ValueError) as error:
            raise FormalCampaignError(
                f"invalid publication record for {directory.name}"
            ) from error
        rows.append((int(match.group("round")), record, raw, directory))
    return rows


def _identity_report(records: Sequence[PublicationRunRecord]) -> dict[str, Any]:
    values = {
        field: sorted({str(getattr(record, field) or "") for record in records})
        for field in _IDENTITY_FIELDS
    }
    mismatches = sorted(field for field, entries in values.items() if len(entries) != 1)
    return {
        "status": "pass" if not mismatches else "fail",
        "mismatches": mismatches,
        "values": {
            field: entries[0] for field, entries in values.items() if len(entries) == 1
        },
    }


def _duration_report(records: Sequence[PublicationRunRecord]) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    for owner in FORMAL_OWNERS:
        for strategy in FORMAL_STRATEGIES:
            durations = [
                record.publication_duration_ms / 1000.0
                for record in records
                if record.owner == owner
                and record.strategy == strategy
                and record.outcome == "committed"
                and record.publication_duration_ms is not None
            ]
            gate = evaluate_duration_gate(durations)
            groups[f"cold/{owner}/production/{strategy}"] = {
                "gate": gate.to_dict(),
                "statistics": duration_statistics(
                    [value * 1000 for value in durations]
                ),
            }
    candidate_statuses = [
        groups[f"cold/{owner}/production/candidate"]["gate"]["status"]
        for owner in FORMAL_OWNERS
    ]
    return {
        "status": "pass"
        if all(status == "pass" for status in candidate_statuses)
        else "fail",
        "groups": groups,
    }


def _paired_report(
    rows: Sequence[tuple[int, PublicationRunRecord, Mapping[str, Any], Path]],
) -> dict[str, Any]:
    by_key: dict[tuple[int, str, str], PublicationRunRecord] = {
        (round_number, record.owner, record.strategy): record
        for round_number, record, _raw, _directory in rows
    }
    owners: dict[str, Any] = {}
    for owner in FORMAL_OWNERS:
        baseline = [
            by_key[(round_number, owner, "baseline")].publication_duration_ms or 0.0
            for round_number in range(1, FORMAL_SAMPLE_COUNT + 1)
        ]
        candidate = [
            by_key[(round_number, owner, "candidate")].publication_duration_ms or 0.0
            for round_number in range(1, FORMAL_SAMPLE_COUNT + 1)
        ]
        owners[owner] = evaluate_paired_improvement(baseline, candidate).to_dict()
    statuses = [bool(value["resolved"]) for value in owners.values()]
    return {
        "status": "pass" if all(statuses) else "insufficient_evidence",
        "owners": owners,
    }


def _trace_report(
    rows: Sequence[tuple[int, PublicationRunRecord, Mapping[str, Any], Path]],
) -> dict[str, Any]:
    accounted = 0
    missing = 0
    for _round_number, record, _raw, directory in rows:
        accounting_path = directory / "trace-accounting.json"
        trace_path = directory / "publication-traces.jsonl"
        if not accounting_path.is_file() or not trace_path.is_file():
            missing += 1
            continue
        accounting = json.loads(accounting_path.read_text(encoding="utf-8"))
        trace_records = cli_support.read_jsonl(trace_path)
        trace_refs = [
            str(item.get("attempt_ref"))
            for item in trace_records
            if item.get("attempt_ref") is not None
        ]
        terminal_refs = accounting.get("terminal_event_attempt_refs", [])
        reconciled = account_terminal_traces(
            publication_attempt_refs=[record.publication_attempt_ref],
            terminal_event_attempt_refs=terminal_refs,
        )
        if (
            trace_refs.count(record.publication_attempt_ref) == 1
            and len(trace_records) == 1
            and reconciled.is_accounted
        ):
            accounted += 1
    return {
        "status": "pass" if accounted == len(rows) and missing == 0 else "fail",
        "records": len(rows),
        "accounted": accounted,
        "missing_artifacts": missing,
    }


def _validate_sample_artifacts(
    rows: Sequence[tuple[int, PublicationRunRecord, Mapping[str, Any], Path]],
) -> list[str]:
    failures: list[str] = []
    for round_number, record, _raw, directory in rows:
        match = _RUN_PATTERN.fullmatch(directory.name)
        if match is None or (
            int(match.group("round")) != round_number
            or match.group("owner") != record.owner
            or match.group("strategy") != record.strategy
        ):
            failures.append(
                "run record matrix identity does not match its clone directory"
            )
        try:
            clone = read_clone_record(directory / "clone.json")
        except (OSError, ValueError, KeyError):
            failures.append("clone identity record is missing or invalid")
            continue
        template_digest = clone.database_identity.get("template_content_digest")
        if (
            clone.run_id != record.run_id
            or clone.schema_revision != "2b3c4d5e6f70"
            or clone.profile != record.postgres_profile
            or clone.postgres_version != record.postgres_version
            or clone.settings_digest() != record.postgres_settings_digest
            or template_digest != record.template_content_digest
        ):
            failures.append("clone identity does not match the formal sample record")
    return sorted(set(failures))


def _sql_report(
    records: Sequence[tuple[int, PublicationRunRecord, Mapping[str, Any], Path]],
) -> dict[str, Any]:
    completeness: dict[str, dict[str, int]] = {}
    for owner in FORMAL_OWNERS:
        for strategy in FORMAL_STRATEGIES:
            group = [
                raw
                for _round, record, raw, _directory in records
                if record.owner == owner and record.strategy == strategy
            ]
            counts = Counter(
                str(
                    bool(raw.get("sql_observation", {}).get("write_row_count_complete"))
                )
                for raw in group
            )
            completeness[f"{owner}/{strategy}"] = {
                key: counts.get(key, 0) for key in ("True", "False")
            }
    unresolved = [key for key, value in completeness.items() if value["False"]]
    return {
        "status": "pass" if not unresolved else "insufficient_evidence",
        "write_row_count_complete": completeness,
        "unresolved_groups": unresolved,
        "reason": "async SQL row counts were incomplete in the formal records"
        if unresolved
        else None,
    }


def build_formal_campaign_report(
    *,
    clones_root: Path,
    repository_root: Path,
    generated_at: str,
) -> dict[str, Any]:
    """Build a redaction-safe campaign report from split formal samples."""
    rows = _read_records(clones_root)
    records = [record for _round, record, _raw, _directory in rows]
    expected_keys = {
        (round_number, owner, strategy)
        for round_number in range(1, FORMAL_SAMPLE_COUNT + 1)
        for owner in FORMAL_OWNERS
        for strategy in FORMAL_STRATEGIES
    }
    actual_keys = {
        (round_number, record.owner, record.strategy)
        for round_number, record, _raw, _directory in rows
    }
    structure_failures: list[str] = []
    if actual_keys != expected_keys or len(rows) != len(expected_keys):
        structure_failures.append(
            "formal campaign must contain exactly 59 paired samples per owner"
        )
    for _round_number, record, raw, directory in rows:
        counts = raw.get("counts")
        if record.run_id != directory.name:
            structure_failures.append(
                "run record identity does not match its clone directory"
            )
        if (
            record.strategy not in FORMAL_STRATEGIES
            or record.owner not in FORMAL_OWNERS
        ):
            structure_failures.append(
                "run record owner or strategy is outside the formal matrix"
            )
        if record.outcome != "committed" or record.publication_duration_ms is None:
            structure_failures.append("formal samples must be committed and timed")
        if raw.get("trace_enabled") is not True:
            structure_failures.append(
                "formal samples must have trace accounting enabled"
            )
        if record.postgres_profile != "production":
            structure_failures.append(
                "formal samples must use the production PostgreSQL profile"
            )
        if not isinstance(counts, Mapping) or any(
            counts.get(field) != expected
            for field, expected in _EXPECTED_COUNTS.items()
        ):
            structure_failures.append(
                "formal sample publication counts do not match the frozen corpus"
            )
    structure_failures.extend(_validate_sample_artifacts(rows))
    structure_failures = sorted(set(structure_failures))
    identity = (
        _identity_report(records)
        if records
        else {"status": "fail", "mismatches": ["no_records"], "values": {}}
    )
    current_source_digest = resolve_source_content_digest(repository_root)
    current_commit = cli_support.resolve_code_commit(repository_root)
    historical_source_digest = (
        next(iter({record.source_content_digest for record in records}), None)
        if records
        else None
    )
    duration = (
        _duration_report(records)
        if not structure_failures
        else {"status": "fail", "groups": {}}
    )
    paired = (
        _paired_report(rows)
        if not structure_failures
        else {"status": "fail", "owners": {}}
    )
    trace = _trace_report(rows)
    sql = _sql_report(rows)
    result: dict[str, Any] = {
        "schema_version": FORMAL_CAMPAIGN_SCHEMA_VERSION,
        "generated_at": generated_at,
        "sample_count": len(rows),
        "structure": {
            "status": "pass" if not structure_failures else "fail",
            "failures": structure_failures,
            "samples_per_owner_strategy": FORMAL_SAMPLE_COUNT,
        },
        "historical_identity": {
            "code_commit": next(iter({record.code_commit for record in records}), None)
            if records
            else None,
            "source_content_digest": historical_source_digest,
            "template_content_digest": next(
                iter({record.template_content_digest for record in records}), None
            )
            if records
            else None,
            "current_code_commit": current_commit,
            "current_source_content_digest": current_source_digest,
            "source_matches_current_tree": historical_source_digest
            == current_source_digest,
        },
        "identity": identity,
        "duration": duration,
        "paired_improvement": paired,
        "trace_accounting": trace,
        "sql_observation": sql,
        "state_parity": {
            "status": "insufficient_evidence",
            "reason": "formal samples use independent generated scopes and all samples have tracing enabled",
        },
        "capacity": {
            "status": "insufficient_evidence",
            "reason": "capacity is a separate campaign and is not inferred from cold publication samples",
        },
        "retrieval_interference": {
            "status": "insufficient_evidence",
            "reason": "retrieval probe evidence is a separate gate",
        },
    }
    unresolved = [
        result[key]["status"]
        for key in (
            "state_parity",
            "capacity",
            "retrieval_interference",
            "sql_observation",
        )
    ]
    result["status"] = (
        "fail"
        if any(
            value == "fail"
            for value in (
                identity["status"],
                duration["status"],
                paired["status"],
                trace["status"],
                result["structure"]["status"],
            )
        )
        else (
            "insufficient_evidence"
            if any(value == "insufficient_evidence" for value in unresolved)
            else "pass"
        )
    )
    assert_report_is_redacted(result)
    return result


def write_formal_campaign_report(
    *, clones_root: Path, repository_root: Path, report_path: Path, generated_at: str
) -> dict[str, Any]:
    """Build and write the redaction-safe formal campaign report."""
    result = build_formal_campaign_report(
        clones_root=clones_root,
        repository_root=repository_root,
        generated_at=generated_at,
    )
    cli_support.write_json(report_path, result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate a split formal publication campaign"
    )
    parser.add_argument("--clones-root", type=Path, required=True)
    parser.add_argument("--report-path", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        result = write_formal_campaign_report(
            clones_root=arguments.clones_root,
            repository_root=BenchmarkLayout.from_path().repository_root,
            report_path=arguments.report_path,
            generated_at=cli_support.utc_now_iso(),
        )
    except (FormalCampaignError, OSError, ValueError, json.JSONDecodeError) as error:
        cli_support.print_result(
            {
                "command": "formal_campaign_report",
                "status": "failed",
                "error": str(error),
            }
        )
        return cli_support.EXIT_GATE_FAILED
    cli_support.print_result(
        {
            "command": "formal_campaign_report",
            "status": result["status"],
            "sample_count": result["sample_count"],
        }
    )
    return (
        cli_support.EXIT_OK
        if result["status"] == "pass"
        else cli_support.EXIT_GATE_FAILED
    )


if __name__ == "__main__":
    raise SystemExit(main())
