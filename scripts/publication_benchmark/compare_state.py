"""Compare traced and untraced publication state after independent samples."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.layout import (  # noqa: E402
    BenchmarkLayout,
    validate_run_id,
)
from scripts.publication_benchmark.state_parity import (  # noqa: E402
    StateParityResult,
    compare_publication_state,
    write_state_parity_artifact,
)
from scripts.publication_benchmark.run_record import (  # noqa: E402
    PublicationRunRecord,
    RunRecordError,
)

PAIR_MATCHING_FIELDS: tuple[str, ...] = (
    "owner",
    "strategy",
    "mode",
    "code_commit",
    "source_content_digest",
    "dependency_lock_digest",
    "postgres_profile",
    "postgres_version",
    "postgres_settings_digest",
    "clone_source_digest",
    "host_id",
    "redis_namespace",
    "input_digest",
    "template_content_digest",
)


class StateComparisonError(ValueError):
    """Raised when the requested pair has no safe comparison evidence."""


def compare_run_samples(
    *,
    layout: BenchmarkLayout,
    run_id: str,
    disabled_sample_id: str,
    enabled_sample_id: str,
) -> StateParityResult:
    """Compare two recorded samples and persist only redacted parity evidence."""
    resolved_run_id = validate_run_id(run_id)
    records = cli_support.read_jsonl(layout.publication_record_path(resolved_run_id))
    disabled = _select_sample(
        records,
        disabled_sample_id,
        run_id=resolved_run_id,
        trace_enabled=False,
    )
    enabled = _select_sample(
        records,
        enabled_sample_id,
        run_id=resolved_run_id,
        trace_enabled=True,
    )
    disabled_state = _read_sample_state(layout, resolved_run_id, disabled_sample_id)
    enabled_state = _read_sample_state(layout, resolved_run_id, enabled_sample_id)
    semantic_result = compare_publication_state(
        disabled_snapshot=disabled_state,
        enabled_snapshot=enabled_state,
        disabled_input_digest=str(disabled.get("input_digest") or ""),
        enabled_input_digest=str(enabled.get("input_digest") or ""),
    )
    mismatches = set(semantic_result.mismatches)
    for field in PAIR_MATCHING_FIELDS:
        if disabled.get(field) != enabled.get(field):
            mismatches.add(f"run_record.{field}")
    result = StateParityResult(
        status="pass" if not mismatches else "fail",
        mismatches=tuple(sorted(mismatches)),
        input_digest=semantic_result.input_digest,
        component_count=semantic_result.component_count,
        disabled_fingerprints=semantic_result.disabled_fingerprints,
        enabled_fingerprints=semantic_result.enabled_fingerprints,
    )
    report_path = state_parity_artifact_path(
        layout=layout,
        run_id=resolved_run_id,
        disabled_sample_id=disabled_sample_id,
        enabled_sample_id=enabled_sample_id,
    )
    write_state_parity_artifact(
        path=report_path,
        result=result,
        sample_refs=(disabled_sample_id, enabled_sample_id),
    )
    return result


def state_parity_artifact_path(
    *,
    layout: BenchmarkLayout,
    run_id: str,
    disabled_sample_id: str,
    enabled_sample_id: str,
) -> Path:
    """Return a stable artifact path that preserves every compared pair."""
    pair_digest = hashlib.sha256(
        f"{disabled_sample_id}\0{enabled_sample_id}".encode("utf-8")
    ).hexdigest()[:24]
    return (
        layout.report_directory(validate_run_id(run_id))
        / "parity"
        / (f"pair-{pair_digest}.json")
    )


def _select_sample(
    records: Sequence[Mapping[str, Any]],
    sample_id: str,
    *,
    run_id: str,
    trace_enabled: bool,
) -> Mapping[str, Any]:
    if not sample_id or Path(sample_id).name != sample_id or ".." in sample_id:
        raise StateComparisonError("sample ID must be a safe file name")
    matches = [record for record in records if record.get("sample_id") == sample_id]
    if len(matches) != 1:
        raise StateComparisonError("sample ID must identify exactly one run record")
    record = matches[0]
    try:
        parsed = PublicationRunRecord.from_dict(record)
    except (KeyError, TypeError, ValueError, RunRecordError) as error:
        raise StateComparisonError("sample run record is incomplete") from error
    if parsed.run_id != run_id:
        raise StateComparisonError("sample run ID does not match the report run")
    if record.get("trace_enabled") is not trace_enabled:
        raise StateComparisonError("sample trace mode does not match comparison side")
    if parsed.outcome != "committed":
        raise StateComparisonError("state parity requires committed samples")
    return record


def _read_sample_state(
    layout: BenchmarkLayout,
    run_id: str,
    sample_id: str,
) -> Mapping[str, Any]:
    path = layout.clone_directory(run_id) / "samples" / sample_id / "state-after.json"
    if not path.is_file():
        raise StateComparisonError("sample state-after artifact is missing")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise StateComparisonError("sample state-after artifact must be an object")
    return loaded


def main(argv: Sequence[str] | None = None) -> int:
    """Run the state parity comparison from its stable CLI interface."""
    parser = argparse.ArgumentParser(description="Compare publication state parity")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--disabled-sample-id", required=True)
    parser.add_argument("--enabled-sample-id", required=True)
    arguments = parser.parse_args(argv)
    try:
        result = compare_run_samples(
            layout=BenchmarkLayout.from_path(),
            run_id=arguments.run_id,
            disabled_sample_id=arguments.disabled_sample_id,
            enabled_sample_id=arguments.enabled_sample_id,
        )
    except (StateComparisonError, ValueError, OSError, json.JSONDecodeError) as error:
        cli_support.print_result(
            {"command": "compare_state", "status": "failed", "error": str(error)}
        )
        return 1
    cli_support.print_result(
        {
            "command": "compare_state",
            **result.to_dict(),
            "artifact_path": str(
                state_parity_artifact_path(
                    layout=BenchmarkLayout.from_path(),
                    run_id=arguments.run_id,
                    disabled_sample_id=arguments.disabled_sample_id,
                    enabled_sample_id=arguments.enabled_sample_id,
                )
            ),
        }
    )
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
