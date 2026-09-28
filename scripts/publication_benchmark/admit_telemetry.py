"""Evaluate available Phase 1 telemetry admission evidence without a database.

The evaluator is deliberately conservative: missing neutral SQL/write counters,
longitudinal memory data, or pool-reuse evidence remains insufficient. A state
parity artifact or a fast traced sample alone never admits telemetry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.check_memory_growth import (  # noqa: E402
    SCHEMA_VERSION as MEMORY_GROWTH_SCHEMA_VERSION,
    artifact_path as memory_artifact_path,
    evaluate_memory_series,
)
from scripts.publication_benchmark.check_pool_reuse import (  # noqa: E402
    POOL_REUSE_SCHEMA_VERSION,
)
from scripts.publication_benchmark.compare_state import (  # noqa: E402
    PAIR_MATCHING_FIELDS,
    state_parity_artifact_path,
)
from scripts.publication_benchmark.layout import (  # noqa: E402
    BenchmarkLayout,
    validate_run_id,
)
from scripts.publication_benchmark.report import (  # noqa: E402
    assert_report_is_redacted,
    percentile,
)
from scripts.publication_benchmark.run_record import (  # noqa: E402
    PublicationRunRecord,
    SOURCE_DIGEST_PATTERN,
)
from scripts.publication_benchmark.state_parity import (  # noqa: E402
    SEMANTIC_COMPONENTS,
    STATE_PARITY_SCHEMA_VERSION,
    compare_publication_state,
)
from scripts.publication_benchmark.state_snapshot import (  # noqa: E402
    opaque_reference,
)
from scripts.publication_benchmark.trace_accounting import (  # noqa: E402
    account_terminal_traces,
)

TELEMETRY_ADMISSION_SCHEMA_VERSION: str = "publication-telemetry-admission/1"
REQUIRED_EXPLORATORY_PAIRS_PER_OWNER: int = 20
REQUIRED_OWNERS: tuple[str, ...] = ("sync", "async")
MEMORY_IDENTITY_FIELDS: tuple[str, ...] = (
    "code_commit",
    "source_content_digest",
    "dependency_lock_digest",
    "input_digest",
    "strategy",
)
TARGET_MEMORY_IDENTITY_FIELDS: tuple[str, ...] = (
    "code_commit",
    "source_content_digest",
    "dependency_lock_digest",
    "strategy",
)
MEMORY_CLONE_FIELDS: tuple[str, ...] = (
    "schema_revision",
    "postgres_profile",
    "postgres_version",
    "postgres_settings_digest",
    "clone_source_digest",
)


def evaluate_telemetry_admission(
    *,
    layout: BenchmarkLayout,
    run_id: str,
    memory_run_id: str | None = None,
    pool_run_id: str | None = None,
) -> dict[str, Any]:
    """Write an admission artifact from recorded samples and parity evidence."""
    resolved_run_id = validate_run_id(run_id)
    clone_directory = layout.clone_directory(resolved_run_id)
    records = cli_support.read_jsonl(layout.publication_record_path(resolved_run_id))
    traces = cli_support.read_jsonl(clone_directory / "publication-traces.jsonl")
    pairs, parity_gate = _load_parity_pairs(
        records,
        layout=layout,
        run_id=resolved_run_id,
    )
    gates: list[dict[str, Any]] = [
        parity_gate,
        _evaluate_terminal_traces(records, traces),
        _evaluate_stage_counters(records, traces),
        _evaluate_duration_overhead(pairs),
        _evaluate_sql_write_parity(pairs),
        _evaluate_memory_growth(
            layout=layout,
            run_id=resolved_run_id,
            memory_run_id=memory_run_id,
            records=records,
        ),
        _evaluate_pool_reuse(
            layout=layout,
            run_id=resolved_run_id,
            pool_run_id=pool_run_id,
            records=records,
        ),
    ]
    statuses = {str(gate["status"]) for gate in gates}
    status = (
        "fail"
        if "fail" in statuses
        else "insufficient_evidence"
        if "insufficient_evidence" in statuses
        else "pass"
    )
    result = {
        "schema_version": TELEMETRY_ADMISSION_SCHEMA_VERSION,
        "run_id": resolved_run_id,
        "memory_run_id": memory_run_id,
        "pool_run_id": pool_run_id,
        "status": status,
        "exploratory_pair_minimum_per_owner": REQUIRED_EXPLORATORY_PAIRS_PER_OWNER,
        "policy_note": (
            "20 cold pairs per owner is the exploratory minimum for this "
            "telemetry evaluator; the final duration rule requires 59 samples"
        ),
        "gates": gates,
    }
    assert_report_is_redacted(result)
    cli_support.write_json(
        layout.report_directory(resolved_run_id) / "telemetry-admission.json",
        result,
    )
    return result


def _evaluate_pool_reuse(
    *,
    layout: BenchmarkLayout,
    run_id: str,
    pool_run_id: str | None,
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Admit only real pooled reuse from the target revision and lockfile."""
    details: dict[str, Any] = {
        "target_run_id": run_id,
        "pool_run_id": pool_run_id,
    }
    if pool_run_id is None or validate_run_id(pool_run_id) == run_id:
        details["reason"] = "provide a separate small-test --pool-run-id"
        return {
            "gate": "pooled-connection-state",
            "status": "insufficient_evidence",
            "details": details,
        }
    identities = {
        (
            str(record.get("code_commit") or ""),
            str(record.get("source_content_digest") or ""),
            str(record.get("dependency_lock_digest") or ""),
        )
        for record in records
        if record.get("mode") == "cold" and record.get("outcome") == "committed"
    }
    if (
        len(identities) != 1
        or not all(next(iter(identities)))
        or not SOURCE_DIGEST_PATTERN.fullmatch(next(iter(identities))[1])
    ):
        details["reason"] = "target committed cold records lack one revision identity"
        return {
            "gate": "pooled-connection-state",
            "status": "insufficient_evidence",
            "details": details,
        }
    path = layout.report_directory(pool_run_id) / "pool-reuse.json"
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        details["reason"] = "pool reuse artifact is missing or invalid"
        return {
            "gate": "pooled-connection-state",
            "status": "insufficient_evidence",
            "details": details,
        }
    expected_commit, expected_source, expected_lock = next(iter(identities))
    if (
        not isinstance(artifact, Mapping)
        or artifact.get("schema_version") != POOL_REUSE_SCHEMA_VERSION
        or artifact.get("run_id") != pool_run_id
        or artifact.get("code_commit") != expected_commit
        or artifact.get("source_content_digest") != expected_source
        or artifact.get("dependency_lock_digest") != expected_lock
    ):
        details["reason"] = "pool reuse artifact revision identity does not match"
        return {
            "gate": "pooled-connection-state",
            "status": "insufficient_evidence",
            "details": details,
        }
    cases = artifact.get("cases")
    if not isinstance(cases, list) or len(cases) != len(REQUIRED_OWNERS):
        details["reason"] = "pool reuse artifact lacks both owner cases"
        return {
            "gate": "pooled-connection-state",
            "status": "insufficient_evidence",
            "details": details,
        }
    expected_drivers = {"sync": "psycopg2", "async": "asyncpg"}
    cases_by_owner = {
        str(case["owner"]): case
        for case in cases
        if isinstance(case, Mapping) and isinstance(case.get("owner"), str)
    }
    if len(cases_by_owner) != len(REQUIRED_OWNERS) or set(cases_by_owner) != set(
        REQUIRED_OWNERS
    ):
        details["reason"] = "pool reuse artifact owner cases are incomplete"
        return {
            "gate": "pooled-connection-state",
            "status": "insufficient_evidence",
            "details": details,
        }
    count_fields = (
        "residual_trace_checkouts",
        "first_trace_statement_count",
        "first_trace_statement_count_after_untraced",
        "first_trace_statement_count_after_reuse",
        "second_trace_statement_count",
    )
    if any(
        case.get("driver") != expected_drivers[owner]
        or not _is_nonnegative_int(case.get("checkout_count"))
        or any(not _is_nonnegative_int(case.get(field)) for field in count_fields)
        for owner, case in cases_by_owner.items()
    ):
        details["reason"] = "pool reuse case has incomplete observations"
        return {
            "gate": "pooled-connection-state",
            "status": "insufficient_evidence",
            "details": details,
        }
    leaked_owners = [
        owner
        for owner, case in cases_by_owner.items()
        if case["residual_trace_checkouts"] > 0
        or case["first_trace_statement_count_after_untraced"]
        > case["first_trace_statement_count"]
        or case["first_trace_statement_count_after_reuse"]
        > case["first_trace_statement_count"]
        or case["second_trace_statement_count"] > 1
    ]
    incomplete_owners = [
        owner
        for owner, case in cases_by_owner.items()
        if case.get("same_backend_reused") is not True
        or case["checkout_count"] != 3
        or case["first_trace_statement_count"] != 1
        or case["first_trace_statement_count_after_untraced"] != 1
        or case["first_trace_statement_count_after_reuse"] != 1
        or case["second_trace_statement_count"] != 1
    ]
    details["leaked_owners"] = sorted(leaked_owners)
    details["incomplete_owners"] = sorted(incomplete_owners)
    return {
        "gate": "pooled-connection-state",
        "status": (
            "fail"
            if leaked_owners
            else "insufficient_evidence"
            if incomplete_owners or artifact.get("status") != "pass"
            else "pass"
        ),
        "details": details,
    }


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _evaluate_memory_growth(
    *,
    layout: BenchmarkLayout,
    run_id: str,
    memory_run_id: str | None,
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Require four complete same-revision process series, one per arm."""
    if memory_run_id is None or validate_run_id(memory_run_id) == run_id:
        return {
            "gate": "memory-growth",
            "status": "insufficient_evidence",
            "details": {
                "target_run_id": run_id,
                "memory_run_id": memory_run_id,
                "reason": "provide a separate small-test --memory-run-id",
            },
        }
    cold_identities = {
        tuple(str(record.get(field) or "") for field in TARGET_MEMORY_IDENTITY_FIELDS)
        for record in records
        if record.get("mode") == "cold" and record.get("outcome") == "committed"
    }
    expected_identity = (
        next(iter(cold_identities)) if len(cold_identities) == 1 else None
    )
    missing_arms: list[str] = []
    invalid_arms: list[str] = []
    failed_arms: list[str] = []
    passed_arms: list[str] = []
    memory_identities: set[tuple[str, ...]] = set()
    clone_identities: set[tuple[str, ...]] = set()
    process_markers: set[tuple[int, str]] = set()
    for owner in REQUIRED_OWNERS:
        for trace_enabled in (False, True):
            arm = f"{owner}:{'enabled' if trace_enabled else 'disabled'}"
            path = memory_artifact_path(
                layout, run_id=memory_run_id, owner=owner, trace_enabled=trace_enabled
            )
            if not path.is_file():
                missing_arms.append(arm)
                continue
            try:
                artifact = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                invalid_arms.append(arm)
                continue
            if not isinstance(artifact, Mapping):
                invalid_arms.append(arm)
                continue
            observed_gate = evaluate_memory_series(artifact)
            stored_gate = artifact.get("gate")
            identity = tuple(
                str(artifact.get(field) or "") for field in MEMORY_IDENTITY_FIELDS
            )
            clone_identity = tuple(
                str(artifact.get(field) or "") for field in MEMORY_CLONE_FIELDS
            )
            process_id = artifact.get("process_id")
            series_started_at = artifact.get("series_started_at")
            if (
                artifact.get("schema_version") != MEMORY_GROWTH_SCHEMA_VERSION
                or artifact.get("run_id") != memory_run_id
                or artifact.get("owner") != owner
                or artifact.get("trace_enabled") is not trace_enabled
                or expected_identity is None
                or not all(expected_identity)
                or not SOURCE_DIGEST_PATTERN.fullmatch(
                    expected_identity[
                        TARGET_MEMORY_IDENTITY_FIELDS.index("source_content_digest")
                    ]
                )
                or tuple(
                    str(artifact.get(field) or "")
                    for field in TARGET_MEMORY_IDENTITY_FIELDS
                )
                != expected_identity
                or not all(identity)
                or not all(clone_identity)
                or not isinstance(process_id, int)
                or process_id <= 0
                or not isinstance(series_started_at, str)
                or not series_started_at
                or not isinstance(stored_gate, Mapping)
                or stored_gate.get("status") != observed_gate["status"]
            ):
                invalid_arms.append(arm)
                continue
            clone_identities.add(clone_identity)
            memory_identities.add(identity)
            process_marker = (process_id, series_started_at)
            if process_marker in process_markers:
                invalid_arms.append(arm)
                continue
            process_markers.add(process_marker)
            if observed_gate["status"] == "pass":
                passed_arms.append(arm)
            elif observed_gate["status"] == "fail":
                failed_arms.append(arm)
            else:
                invalid_arms.append(arm)
    if len(clone_identities) > 1:
        invalid_arms.append("clone-identity-mismatch")
    if len(memory_identities) > 1:
        invalid_arms.append("memory-input-identity-mismatch")
    status = (
        "fail"
        if failed_arms
        else "insufficient_evidence"
        if missing_arms or invalid_arms or len(passed_arms) != 4
        else "pass"
    )
    return {
        "gate": "memory-growth",
        "status": status,
        "details": {
            "target_run_id": run_id,
            "memory_run_id": memory_run_id,
            "memory_input_digest": (
                next(iter(memory_identities))[
                    MEMORY_IDENTITY_FIELDS.index("input_digest")
                ]
                if len(memory_identities) == 1
                else None
            ),
            "passed_arms": passed_arms,
            "failed_arms": failed_arms,
            "missing_arms": missing_arms,
            "invalid_arms": invalid_arms,
            "reason": (
                "post-GC current RSS from a separate small-test clone; code, "
                "lock, and strategy match the target publication run"
            ),
        },
    }


def _load_parity_pairs(
    records: Sequence[Mapping[str, Any]],
    *,
    layout: BenchmarkLayout,
    run_id: str,
) -> tuple[list[tuple[Mapping[str, Any], Mapping[str, Any]]], dict[str, Any]]:
    record_by_ref: dict[str, Mapping[str, Any]] = {}
    duplicate_records = 0
    for record in records:
        sample_id = record.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id:
            continue
        reference = opaque_reference(sample_id)
        if reference in record_by_ref:
            duplicate_records += 1
        record_by_ref[reference] = record
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    failures = duplicate_records
    seen: set[str] = set()
    paths = sorted((layout.report_directory(run_id) / "parity").glob("pair-*.json"))
    for path in paths:
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            failures += 1
            continue
        refs = artifact.get("sample_refs") if isinstance(artifact, Mapping) else None
        if not isinstance(refs, list) or len(refs) != 2:
            failures += 1
            continue
        disabled = record_by_ref.get(str(refs[0]))
        enabled = record_by_ref.get(str(refs[1]))
        if disabled is None or enabled is None:
            failures += 1
            continue
        try:
            disabled_record = PublicationRunRecord.from_dict(disabled)
            enabled_record = PublicationRunRecord.from_dict(enabled)
            expected_path = state_parity_artifact_path(
                layout=layout,
                run_id=run_id,
                disabled_sample_id=disabled_record.sample_id,
                enabled_sample_id=enabled_record.sample_id,
            )
            disabled_snapshot = _read_state_snapshot(
                layout, run_id, disabled_record.sample_id
            )
            enabled_snapshot = _read_state_snapshot(
                layout, run_id, enabled_record.sample_id
            )
            current_parity = compare_publication_state(
                disabled_snapshot=disabled_snapshot,
                enabled_snapshot=enabled_snapshot,
                disabled_input_digest=disabled_record.input_digest,
                enabled_input_digest=enabled_record.input_digest,
            )
        except (KeyError, TypeError, ValueError, OSError):
            failures += 1
            continue
        if (
            str(refs[0]) in seen
            or str(refs[1]) in seen
            or disabled.get("trace_enabled") is not False
            or enabled.get("trace_enabled") is not True
            or disabled.get("outcome") != "committed"
            or enabled.get("outcome") != "committed"
            or not isinstance(disabled.get("source_content_digest"), str)
            or not SOURCE_DIGEST_PATTERN.fullmatch(
                str(disabled.get("source_content_digest"))
            )
            or artifact.get("status") != "pass"
            or artifact.get("schema_version") != STATE_PARITY_SCHEMA_VERSION
            or artifact.get("input_digest") != disabled_record.input_digest
            or artifact.get("component_count") != len(SEMANTIC_COMPONENTS)
            or artifact.get("mismatches") != []
            or artifact.get("disabled_fingerprints")
            != current_parity.disabled_fingerprints
            or artifact.get("enabled_fingerprints")
            != current_parity.enabled_fingerprints
            or current_parity.status != "pass"
            or expected_path != path
            or disabled_record.run_id != run_id
            or enabled_record.run_id != run_id
            or any(
                disabled.get(field) != enabled.get(field)
                for field in PAIR_MATCHING_FIELDS
            )
        ):
            failures += 1
            continue
        seen.update((str(refs[0]), str(refs[1])))
        pairs.append((disabled, enabled))
    eligible_refs = {
        opaque_reference(record.get("sample_id"))
        for record in records
        if record.get("outcome") == "committed"
        and record.get("mode") == "cold"
        and record.get("trace_enabled") in (True, False)
    }
    unpaired_count = len(eligible_refs - seen)
    status = (
        "fail"
        if failures
        else "insufficient_evidence"
        if not pairs or unpaired_count
        else "pass"
    )
    return pairs, {
        "gate": "publication-state-parity",
        "status": status,
        "details": {
            "paired_samples": len(pairs),
            "invalid_pair_artifacts": failures,
            "duplicate_sample_records": duplicate_records,
            "unpaired_cold_samples": unpaired_count,
        },
    }


def _read_state_snapshot(
    layout: BenchmarkLayout,
    run_id: str,
    sample_id: str,
) -> Mapping[str, Any]:
    if not sample_id or Path(sample_id).name != sample_id or ".." in sample_id:
        raise ValueError("sample ID must be a safe file name")
    path = layout.clone_directory(run_id) / "samples" / sample_id / "state-after.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping):
        raise ValueError("sample state artifact must be an object")
    return loaded


def _evaluate_terminal_traces(
    records: Sequence[Mapping[str, Any]],
    traces: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    expected = [
        str(record["publication_attempt_ref"])
        for record in records
        if record.get("trace_enabled") is True
    ]
    observed = [str(trace.get("attempt_ref")) for trace in traces]
    accounting = account_terminal_traces(
        publication_attempt_refs=expected,
        terminal_event_attempt_refs=observed,
    )
    committed_refs = {
        str(record["publication_attempt_ref"])
        for record in records
        if record.get("trace_enabled") is True and record.get("outcome") == "committed"
    }
    unsuccessful = sum(
        1
        for trace in traces
        if str(trace.get("attempt_ref")) in committed_refs
        and trace.get("outcome") != "success"
    )
    return {
        "gate": "terminal-trace-accounting",
        "status": (
            "insufficient_evidence"
            if not expected
            else "pass"
            if accounting.is_accounted and not unsuccessful
            else "fail"
        ),
        "details": {
            "publication_attempts": accounting.publication_attempts,
            "terminal_events": accounting.terminal_events,
            "missing": len(accounting.missing_attempt_refs),
            "duplicate": len(accounting.duplicate_attempt_refs),
            "unexpected": len(accounting.unexpected_attempt_refs),
            "unsuccessful_committed_events": unsuccessful,
        },
    }


def _evaluate_stage_counters(
    records: Sequence[Mapping[str, Any]],
    traces: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    trace_by_ref = {str(trace.get("attempt_ref")): trace for trace in traces}
    compared = 0
    mismatches = 0
    missing = 0
    for record in records:
        if record.get("trace_enabled") is not True:
            continue
        trace = trace_by_ref.get(str(record.get("publication_attempt_ref")))
        trace_counts = trace.get("counts") if trace is not None else None
        persisted_counts = record.get("counts")
        if not isinstance(trace_counts, Mapping) or not isinstance(
            persisted_counts, Mapping
        ):
            missing += 1
            continue
        pairs = (
            ("chunks", "persisted_document_chunks"),
            ("map_units", "persisted_map_units"),
            ("tokens", "persisted_token_rows"),
        )
        if any(
            trace_counts.get(trace_key) is None
            or persisted_counts.get(record_key) is None
            for trace_key, record_key in pairs
        ):
            missing += 1
            continue
        compared += 1
        if any(
            trace_counts[trace_key] != persisted_counts[record_key]
            for trace_key, record_key in pairs
        ):
            mismatches += 1
    return {
        "gate": "stage-counter-reconciliation",
        "status": (
            "fail"
            if mismatches
            else "insufficient_evidence"
            if missing or compared == 0
            else "pass"
        ),
        "details": {
            "compared_attempts": compared,
            "missing_counter_attempts": missing,
            "mismatched_attempts": mismatches,
        },
    }


def _evaluate_sql_write_parity(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> dict[str, Any]:
    compared = 0
    missing = 0
    mismatches = 0
    for disabled, enabled in pairs:
        disabled_observation = disabled.get("sql_observation")
        enabled_observation = enabled.get("sql_observation")
        if not isinstance(disabled_observation, Mapping) or not isinstance(
            enabled_observation, Mapping
        ):
            missing += 1
            continue
        keys = ("statement_count", "write_statement_count")
        if any(
            not isinstance(observation.get(key), int)
            or isinstance(observation.get(key), bool)
            or observation[key] < 0
            for observation in (disabled_observation, enabled_observation)
            for key in keys
        ):
            missing += 1
            continue
        compared += 1
        if any(disabled_observation[key] != enabled_observation[key] for key in keys):
            mismatches += 1
    return {
        "gate": "sql-write-parity",
        "status": (
            "fail"
            if mismatches
            else "insufficient_evidence"
            if missing or not compared
            else "pass"
        ),
        "details": {
            "compared_pairs": compared,
            "missing_observation_pairs": missing,
            "mismatched_pairs": mismatches,
            "compared_fields": ["statement_count", "write_statement_count"],
        },
    }


def _evaluate_duration_overhead(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> dict[str, Any]:
    grouped: dict[str, tuple[list[float], list[float]]] = {}
    invalid_durations = 0
    for disabled, enabled in pairs:
        if disabled.get("mode") != "cold" or enabled.get("mode") != "cold":
            continue
        try:
            disabled_ms = float(disabled["publication_duration_ms"])
            enabled_ms = float(enabled["publication_duration_ms"])
        except (KeyError, TypeError, ValueError):
            invalid_durations += 1
            continue
        if not 0 <= disabled_ms < float("inf") or not 0 <= enabled_ms < float("inf"):
            invalid_durations += 1
            continue
        comparison_identity = {
            field: disabled.get(field) for field in PAIR_MATCHING_FIELDS
        }
        identity_digest = hashlib.sha256(
            json.dumps(comparison_identity, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        key = (
            "/".join(
                str(disabled.get(field))
                for field in ("owner", "postgres_profile", "strategy")
            )
            + f"/{identity_digest}"
        )
        disabled_values, enabled_values = grouped.setdefault(key, ([], []))
        disabled_values.append(disabled_ms)
        enabled_values.append(enabled_ms)
    results: dict[str, dict[str, Any]] = {}
    for key, (disabled_values, enabled_values) in grouped.items():
        disabled_p95 = percentile(disabled_values, 0.95)
        enabled_p95 = percentile(enabled_values, 0.95)
        threshold_ms = max(disabled_p95 * 0.02, 50.0)
        results[key] = {
            "pair_count": len(disabled_values),
            "disabled_p95_ms": disabled_p95,
            "enabled_p95_ms": enabled_p95,
            "overhead_ms": enabled_p95 - disabled_p95,
            "allowed_overhead_ms": threshold_ms,
            "status": (
                "insufficient_evidence"
                if len(disabled_values) < REQUIRED_EXPLORATORY_PAIRS_PER_OWNER
                else "pass"
                if enabled_p95 - disabled_p95 <= threshold_ms
                else "fail"
            ),
        }
    owners = {key.split("/", 1)[0] for key in results}
    statuses = {entry["status"] for entry in results.values()}
    status = (
        "fail"
        if "fail" in statuses
        else "insufficient_evidence"
        if invalid_durations
        or not set(REQUIRED_OWNERS).issubset(owners)
        or "insufficient_evidence" in statuses
        else "pass"
    )
    return {
        "gate": "cold-p95-overhead",
        "status": status,
        "details": {
            "groups": results,
            "invalid_duration_pairs": invalid_durations,
            "required_owners": list(REQUIRED_OWNERS),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Write a telemetry admission result without running publication."""
    parser = argparse.ArgumentParser(description="Evaluate telemetry admission")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--memory-run-id", default=None)
    parser.add_argument("--pool-run-id", default=None)
    arguments = parser.parse_args(argv)
    try:
        result = evaluate_telemetry_admission(
            layout=BenchmarkLayout.from_path(),
            run_id=arguments.run_id,
            memory_run_id=arguments.memory_run_id,
            pool_run_id=arguments.pool_run_id,
        )
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as error:
        cli_support.print_result(
            {"command": "admit_telemetry", "status": "failed", "error": str(error)}
        )
        return cli_support.EXIT_GATE_FAILED
    cli_support.print_result({"command": "admit_telemetry", **result})
    return cli_support.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
