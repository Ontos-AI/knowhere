"""Run one verification case from the Publication benchmark plan.

ST-001 compares two completed cold publications recorded for the same clone
run, with the clone reset between baseline and candidate samples. Remaining
cases fail loudly until their Phase 3 harnesses exist.

    uv run python scripts/publication_benchmark/run_correctness.py \
      --case LP-001 \
      --owner sync \
      --strategy baseline
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.cases import (  # noqa: E402
    CASE_REGISTRY,
    CaseResult,
    CaseNotImplementedError,
    PLAN_DOCUMENT_RELATIVE_PATH,
    PublicationCaseEvidence,
    ReplacementCaseEvidence,
    find_registry_mismatches,
    require_case,
    run_verification_case,
    unavailable_case_result,
)
from scripts.publication_benchmark.layout import BenchmarkLayout  # noqa: E402
from scripts.publication_benchmark.run_record import (  # noqa: E402
    PUBLICATION_OWNERS,
    PublicationRunRecord,
)
from scripts.publication_benchmark.layout import validate_run_id  # noqa: E402

CASE_RESULT_DIRECTORY_RELATIVE: str = ".benchmarks/publication/reports"


class CaseEvidenceError(ValueError):
    """Raised when a recorded sample cannot support a verification case."""


def read_sample_evidence(
    *,
    layout: BenchmarkLayout,
    run_id: str,
    sample_id: str,
) -> PublicationCaseEvidence:
    """Read one exact sample and its before/after snapshots."""
    validate_run_id(sample_id)
    records = cli_support.read_jsonl(layout.publication_record_path(run_id))
    matching = [record for record in records if record.get("sample_id") == sample_id]
    if len(matching) != 1:
        raise CaseEvidenceError(
            f"sample {sample_id!r} must identify exactly one run record"
        )
    record = PublicationRunRecord.from_dict(matching[0])
    if record.run_id != run_id or record.clone_id != run_id:
        raise CaseEvidenceError("sample run and clone IDs must match the case run")
    sample_directory = layout.clone_directory(run_id) / "samples" / sample_id
    before_path = sample_directory / "state-before.json"
    after_path = sample_directory / "state-after.json"
    if matching[0].get("state_before_file") != str(before_path) or matching[0].get(
        "state_after_file"
    ) != str(after_path):
        raise CaseEvidenceError("sample snapshot paths do not match the run record")
    return PublicationCaseEvidence(
        record=record,
        state_before=_read_snapshot(before_path),
        state_after=_read_snapshot(after_path),
    )


def _read_snapshot(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise CaseEvidenceError(f"sample snapshot is missing: {path.name}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CaseEvidenceError(f"sample snapshot must be an object: {path.name}")
    return payload


def case_result_path(
    *,
    layout: BenchmarkLayout,
    run_id: str,
    case_id: str,
) -> Path:
    """Return the case result artifact path for one run."""
    directory = layout.ensure_report_directory(run_id)
    return directory / f"case-{case_id}.json"


def run_case(
    *,
    layout: BenchmarkLayout,
    case_id: str,
    owner: str,
    strategy: str,
    run_id: str,
    baseline_sample_id: str | None = None,
    candidate_sample_id: str | None = None,
    initial_sample_id: str | None = None,
    duplicate_sample_id: str | None = None,
    baseline_initial_sample_id: str | None = None,
    baseline_replacement_sample_id: str | None = None,
    candidate_initial_sample_id: str | None = None,
    candidate_replacement_sample_id: str | None = None,
) -> tuple[dict[str, object], int]:
    """Run one verification case and return its result document and exit code."""
    case = require_case(case_id)
    result_path = case_result_path(layout=layout, run_id=run_id, case_id=case_id)
    try:
        if case_id == "ST-001" and baseline_sample_id and candidate_sample_id:
            baseline = read_sample_evidence(
                layout=layout, run_id=run_id, sample_id=baseline_sample_id
            )
            candidate = read_sample_evidence(
                layout=layout, run_id=run_id, sample_id=candidate_sample_id
            )
            if baseline.record.owner != owner or candidate.record.owner != owner:
                raise CaseEvidenceError("sample owners must match the requested owner")
            if strategy != "candidate":
                raise CaseEvidenceError("ST-001 requires --strategy candidate")
            result = run_verification_case(
                case_id, baseline=baseline, candidate=candidate
            )
        elif (
            case_id in ("ID-001", "ID-006")
            and initial_sample_id
            and duplicate_sample_id
        ):
            initial = read_sample_evidence(
                layout=layout, run_id=run_id, sample_id=initial_sample_id
            )
            duplicate = read_sample_evidence(
                layout=layout, run_id=run_id, sample_id=duplicate_sample_id
            )
            if initial.record.owner != owner or duplicate.record.owner != owner:
                raise CaseEvidenceError("sample owners must match the requested owner")
            if initial.record.strategy != strategy or duplicate.record.strategy != strategy:
                raise CaseEvidenceError("sample strategies must match the requested strategy")
            result = run_verification_case(
                case_id, initial=initial, duplicate=duplicate
            )
        elif (
            case_id == "ST-002"
            and baseline_initial_sample_id
            and baseline_replacement_sample_id
            and candidate_initial_sample_id
            and candidate_replacement_sample_id
        ):
            baseline_initial = read_sample_evidence(
                layout=layout, run_id=run_id, sample_id=baseline_initial_sample_id
            )
            baseline_replacement = read_sample_evidence(
                layout=layout, run_id=run_id, sample_id=baseline_replacement_sample_id
            )
            candidate_initial = read_sample_evidence(
                layout=layout, run_id=run_id, sample_id=candidate_initial_sample_id
            )
            candidate_replacement = read_sample_evidence(
                layout=layout, run_id=run_id, sample_id=candidate_replacement_sample_id
            )
            evidence = (
                baseline_initial,
                baseline_replacement,
                candidate_initial,
                candidate_replacement,
            )
            if any(sample.record.owner != owner for sample in evidence):
                raise CaseEvidenceError("sample owners must match the requested owner")
            result = run_verification_case(
                case_id,
                baseline_replacement=ReplacementCaseEvidence(
                    initial=baseline_initial, replacement=baseline_replacement
                ),
                candidate_replacement=ReplacementCaseEvidence(
                    initial=candidate_initial, replacement=candidate_replacement
                ),
            )
        else:
            result = run_verification_case(case_id)
    except (CaseEvidenceError, ValueError, KeyError, OSError, json.JSONDecodeError) as error:
        result = CaseResult(
            case_id=case.case_id,
            title=case.title,
            expected_outcome=(
                (
                    "duplicate completion after a successful commit is idempotent"
                    if case_id == "ID-001"
                    else "all-duplicate input commits as a no-op without changing state"
                )
                if case_id in ("ID-001", "ID-006")
                else "comparable cold publications create identical new-document state"
                if case_id != "ST-002"
                else "baseline and candidate replacement revisions have identical persisted state"
            ),
            observed_outcome="invalid_evidence",
            passed=False,
            failure_reason=str(error),
        )
    except CaseNotImplementedError:
        placeholder = unavailable_case_result(case_id).to_dict()
        document = {
            **placeholder,
            "owner": owner,
            "strategy": strategy,
            "run_id": run_id,
        }
        cli_support.write_json(result_path, document)
        cli_support.print_result(
            {
                "command": "run_correctness",
                "case_id": case_id,
                "title": case.title,
                "status": "not_implemented",
                "passed": False,
                "result_file": str(result_path),
                "error": (
                    f"verification case {case_id} has no harness yet; Phase 3 "
                    "must implement it before candidate evidence exists"
                ),
            }
        )
        return document, cli_support.EXIT_NOT_IMPLEMENTED
    document = {
        **result.to_dict(),
        "owner": owner,
        "strategy": strategy,
        "run_id": run_id,
    }
    cli_support.write_json(result_path, document)
    cli_support.print_result(
        {
            "command": "run_correctness",
            "case_id": case_id,
            "status": "pass" if result.passed else "fail",
            "passed": result.passed,
            "result_file": str(result_path),
        }
    )
    return document, (
        cli_support.EXIT_OK if result.passed else cli_support.EXIT_GATE_FAILED
    )


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the run_correctness command line parser."""
    parser = argparse.ArgumentParser(
        description="Run one Publication verification case",
    )
    parser.add_argument("--case", default=None, help="Verification case identifier")
    parser.add_argument("--owner", default="sync", choices=PUBLICATION_OWNERS)
    parser.add_argument(
        "--strategy",
        default="baseline",
        choices=("baseline", "candidate"),
    )
    parser.add_argument("--run-id", default="case-harness")
    parser.add_argument("--baseline-sample-id", default=None)
    parser.add_argument("--candidate-sample-id", default=None)
    parser.add_argument("--initial-sample-id", default=None)
    parser.add_argument("--duplicate-sample-id", default=None)
    parser.add_argument("--baseline-initial-sample-id", default=None)
    parser.add_argument("--baseline-replacement-sample-id", default=None)
    parser.add_argument("--candidate-initial-sample-id", default=None)
    parser.add_argument("--candidate-replacement-sample-id", default=None)
    parser.add_argument(
        "--list",
        action="store_true",
        help="List every documented verification case identifier",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the run_correctness command."""
    arguments = build_argument_parser().parse_args(argv)
    layout = BenchmarkLayout.from_path()
    if arguments.list:
        plan_path = layout.repository_root / PLAN_DOCUMENT_RELATIVE_PATH
        cli_support.print_result(
            {
                "command": "run_correctness",
                "status": "ok",
                "cases": [case.to_dict() for case in CASE_REGISTRY.values()],
                "registry_mismatches": list(
                    find_registry_mismatches(plan_path)
                ),
            }
        )
        return cli_support.EXIT_OK
    if not arguments.case:
        cli_support.print_result(
            {
                "command": "run_correctness",
                "status": "failed",
                "error": "--case is required unless --list is used",
            }
        )
        return cli_support.EXIT_GATE_FAILED
    try:
        _, exit_code = run_case(
            layout=layout,
            case_id=arguments.case,
            owner=arguments.owner,
            strategy=arguments.strategy,
            run_id=arguments.run_id,
            baseline_sample_id=arguments.baseline_sample_id,
            candidate_sample_id=arguments.candidate_sample_id,
            initial_sample_id=arguments.initial_sample_id,
            duplicate_sample_id=arguments.duplicate_sample_id,
            baseline_initial_sample_id=arguments.baseline_initial_sample_id,
            baseline_replacement_sample_id=arguments.baseline_replacement_sample_id,
            candidate_initial_sample_id=arguments.candidate_initial_sample_id,
            candidate_replacement_sample_id=arguments.candidate_replacement_sample_id,
        )
    except KeyError as error:
        cli_support.print_result(
            {
                "command": "run_correctness",
                "status": "failed",
                "error": str(error),
            }
        )
        return cli_support.EXIT_GATE_FAILED
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
