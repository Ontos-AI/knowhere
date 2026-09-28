"""Contract tests for the verification case registry and placeholders."""

# ruff: noqa: E402

from __future__ import annotations

import json
from copy import deepcopy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.support.publication_benchmark_support import (
    PLAN_DOCUMENT_RELATIVE_PATH,
    REPOSITORY_ROOT,
    benchmark_layout,
    copy_plan_document,
    ensure_benchmark_import_path,
)

ensure_benchmark_import_path()

from scripts.publication_benchmark import cli_support  # noqa: E402
from scripts.publication_benchmark.cases import (  # noqa: E402
    CASE_REGISTRY,
    CaseNotImplementedError,
    IMPLEMENTED_CASE_IDS,
    documented_case_ids,
    find_registry_mismatches,
    parse_case_ids,
    require_case,
    run_verification_case,
    unavailable_case_result,
)
from scripts.publication_benchmark.run_correctness import (  # noqa: E402
    build_argument_parser,
    run_case,
)
from scripts.publication_benchmark.run_record import PublicationRunRecord  # noqa: E402
from scripts.publication_benchmark.layout import BenchmarkLayout  # noqa: E402
from scripts.publication_benchmark.state_parity import (  # noqa: E402
    SEMANTIC_COMPONENTS,
)
from scripts.publication_benchmark.state_snapshot import (  # noqa: E402
    SEMANTIC_STATE_SCHEMA_VERSION,
    STATE_SNAPSHOT_SCHEMA_VERSION,
    canonical_semantic_digest,
)

PLAN_PATH = Path(REPOSITORY_ROOT) / PLAN_DOCUMENT_RELATIVE_PATH


def test_case_registry_matches_the_design_document() -> None:
    documented = documented_case_ids(PLAN_PATH)

    assert len(documented) == 30
    assert documented[:5] == ("LP-001", "LP-002", "SI-001", "ST-001", "ST-002")
    assert documented[-3:] == ("PE-001", "PE-002", "PE-003")
    assert find_registry_mismatches(PLAN_PATH) == ()
    assert tuple(CASE_REGISTRY) == documented


def test_case_range_parsing_expands_atomicity_and_strategy_matrix() -> None:
    case_ids = parse_case_ids(
        "```text\n"
        "AT-001..AT-008  failure after each publication stage and before commit\n"
        "SC-001..SC-004  baseline/candidate read-write compatibility matrix\n"
        "```\n"
    )

    assert case_ids == (
        "AT-001",
        "AT-002",
        "AT-003",
        "AT-004",
        "AT-005",
        "AT-006",
        "AT-007",
        "AT-008",
        "SC-001",
        "SC-002",
        "SC-003",
        "SC-004",
    )


def test_every_case_placeholder_fails_loudly() -> None:
    for case_id in CASE_REGISTRY:
        if case_id in IMPLEMENTED_CASE_IDS:
            continue
        result = unavailable_case_result(case_id)
        assert result.passed is False
        assert result.observed_outcome == "not_implemented"
        assert "fails loudly" in str(result.failure_reason)
        with pytest.raises(CaseNotImplementedError, match=case_id):
            run_verification_case(case_id)

    assert run_verification_case("ST-001").observed_outcome == "missing_evidence"
    assert run_verification_case("ST-002").observed_outcome == "missing_evidence"
    assert run_verification_case("ID-001").observed_outcome == "missing_evidence"
    assert run_verification_case("ID-006").observed_outcome == "missing_evidence"
    with pytest.raises(ValueError, match="implemented"):
        unavailable_case_result("ST-001")


def test_unknown_case_identifier_is_rejected() -> None:
    with pytest.raises(KeyError):
        require_case("XX-999")
    with pytest.raises(KeyError):
        unavailable_case_result("XX-999")


def test_run_case_writes_a_failing_result_and_exits_non_zero(
    tmp_path: Path,
) -> None:
    layout = benchmark_layout(tmp_path)
    copy_plan_document(tmp_path)

    document, exit_code = run_case(
        layout=layout,
        case_id="LP-001",
        owner="sync",
        strategy="baseline",
        run_id="case-run",
    )

    assert exit_code == cli_support.EXIT_NOT_IMPLEMENTED
    assert document["passed"] is False
    result_path = layout.report_directory("case-run") / "case-LP-001.json"
    stored = json.loads(result_path.read_text(encoding="utf-8"))
    assert stored["case_id"] == "LP-001"
    assert stored["expected_outcome"].startswith("case harness defined")
    assert stored["observed_outcome"] == "not_implemented"
    assert stored["state_before"] == {}
    assert stored["state_after"] == {}


def test_correctness_cli_requires_a_case_unless_listing() -> None:
    parser = build_argument_parser()

    assert parser.parse_args(["--list"]).list is True
    arguments = parser.parse_args(["--case", "ST-001", "--owner", "async"])
    assert arguments.case == "ST-001"
    assert arguments.owner == "async"
    assert arguments.strategy == "baseline"


def build_record(
    *, sample_id: str, strategy: str, mode: str = "cold"
) -> PublicationRunRecord:
    return PublicationRunRecord(
        run_id="case-run",
        sample_id=sample_id,
        strategy=strategy,
        owner="sync",
        mode=mode,
        code_commit="commit",
        source_content_digest=canonical_semantic_digest("source"),
        dependency_lock_digest=canonical_semantic_digest("lock"),
        postgres_profile="production",
        postgres_version="16",
        postgres_settings_digest=canonical_semantic_digest("settings"),
        redis_namespace="publication-benchmark:case-run",
        input_digest=canonical_semantic_digest("input"),
        clone_id="case-run",
        clone_source_digest=canonical_semantic_digest("clone"),
        host_id="host-opaque",
        started_at="2026-09-25T00:00:00Z",
        ended_at="2026-09-25T00:00:01Z",
        outcome="committed",
        publication_duration_ms=1000.0,
        publication_attempt_ref=f"attempt-{sample_id}",
        template_content_digest=canonical_semantic_digest("template"),
    )


def build_case_snapshot(*, populated: bool) -> dict[str, Any]:
    counts = {
        "documents_active": 1 if populated else 0,
        "documents_archived": 0,
        "document_sections": 1 if populated else 0,
        "document_chunks": 1 if populated else 0,
        "document_map_units": 1 if populated else 0,
        "document_map_unit_tokens": 1 if populated else 0,
        "document_map_unit_indexes": 1 if populated else 0,
        "graph_nodes": 1 if populated else 0,
        "graph_edges": 0,
    }
    return {
        "schema_version": STATE_SNAPSHOT_SCHEMA_VERSION,
        "scope_ref": "scope-opaque",
        "relation_counts": counts,
        "chunk_type_counts": {"text": 1} if populated else {},
        "namespace_generation": 1 if populated else None,
        "namespace_snapshot": {"checksum": "opaque"} if populated else None,
        "serving_revision_manifests": [{"revision_ref": "ref-revision"}]
        if populated
        else [],
        "documents": [{"document_ref": "ref-document"}] if populated else [],
        "source_file_name_refs": ["ref-filename"] if populated else [],
        "semantic_fingerprints": {
            "schema_version": SEMANTIC_STATE_SCHEMA_VERSION,
            **{
                component: canonical_semantic_digest(component)
                for component in SEMANTIC_COMPONENTS
            },
        },
    }


def write_case_sample(
    layout: BenchmarkLayout,
    *,
    sample_id: str,
    strategy: str,
    mode: str = "cold",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    record = build_record(sample_id=sample_id, strategy=strategy, mode=mode)
    sample_path = layout.clone_directory("case-run") / "samples" / sample_id
    before_path = sample_path / "state-before.json"
    after_path = sample_path / "state-after.json"
    cli_support.append_jsonl(
        layout.publication_record_path("case-run"),
        {
            **record.to_dict(),
            "state_before_file": str(before_path),
            "state_after_file": str(after_path),
        },
    )
    cli_support.write_json(
        before_path,
        before or build_case_snapshot(populated=False),
    )
    cli_support.write_json(
        after_path,
        after or build_case_snapshot(populated=True),
    )


def test_new_document_case_compares_recorded_cold_samples(tmp_path: Path) -> None:
    layout = benchmark_layout(tmp_path)
    write_case_sample(layout, sample_id="baseline-sample", strategy="baseline")
    write_case_sample(layout, sample_id="candidate-sample", strategy="candidate")

    result, exit_code = run_case(
        layout=layout,
        case_id="ST-001",
        owner="sync",
        strategy="candidate",
        run_id="case-run",
        baseline_sample_id="baseline-sample",
        candidate_sample_id="candidate-sample",
    )

    assert exit_code == cli_support.EXIT_OK
    assert result["observed_outcome"] == "parity"
    assert result["passed"] is True
    before = result["state_before"]
    after = result["state_after"]
    assert isinstance(before, dict)
    assert isinstance(after, dict)
    assert before["baseline"]["relation_counts"]["documents_active"] == 0
    assert after["candidate"]["relation_counts"]["documents_active"] == 1


def test_new_document_case_rejects_semantic_mismatch(tmp_path: Path) -> None:
    layout = benchmark_layout(tmp_path)
    write_case_sample(layout, sample_id="baseline-sample", strategy="baseline")
    changed = build_case_snapshot(populated=True)
    changed["semantic_fingerprints"]["chunks"] = canonical_semantic_digest("changed")
    write_case_sample(
        layout, sample_id="candidate-sample", strategy="candidate", after=changed
    )

    result, exit_code = run_case(
        layout=layout,
        case_id="ST-001",
        owner="sync",
        strategy="candidate",
        run_id="case-run",
        baseline_sample_id="baseline-sample",
        candidate_sample_id="candidate-sample",
    )

    assert exit_code == cli_support.EXIT_GATE_FAILED
    assert "state.chunks" in str(result["failure_reason"])


def test_new_document_case_rejects_missing_sample(tmp_path: Path) -> None:
    result, exit_code = run_case(
        layout=benchmark_layout(tmp_path),
        case_id="ST-001",
        owner="sync",
        strategy="candidate",
        run_id="case-run",
        baseline_sample_id="missing-sample",
        candidate_sample_id="candidate-sample",
    )

    assert exit_code == cli_support.EXIT_GATE_FAILED
    assert result["observed_outcome"] == "invalid_evidence"


def test_all_duplicate_case_requires_no_state_change(tmp_path: Path) -> None:
    layout = benchmark_layout(tmp_path)
    initial_after = build_case_snapshot(populated=True)
    write_case_sample(
        layout,
        sample_id="initial-sample",
        strategy="baseline",
        mode="cold",
        after=initial_after,
    )
    write_case_sample(
        layout,
        sample_id="duplicate-sample",
        strategy="baseline",
        mode="warm",
        before=initial_after,
        after=initial_after,
    )

    result, exit_code = run_case(
        layout=layout,
        case_id="ID-006",
        owner="sync",
        strategy="baseline",
        run_id="case-run",
        initial_sample_id="initial-sample",
        duplicate_sample_id="duplicate-sample",
    )

    assert exit_code == cli_support.EXIT_OK
    assert result["observed_outcome"] == "no_op"
    assert result["passed"] is True


def test_all_duplicate_case_rejects_state_change(tmp_path: Path) -> None:
    layout = benchmark_layout(tmp_path)
    initial_after = build_case_snapshot(populated=True)
    write_case_sample(
        layout,
        sample_id="initial-sample",
        strategy="baseline",
        mode="cold",
        after=initial_after,
    )
    changed_after = build_case_snapshot(populated=True)
    changed_after["relation_counts"]["document_chunks"] = 2
    write_case_sample(
        layout,
        sample_id="duplicate-sample",
        strategy="baseline",
        mode="warm",
        before=initial_after,
        after=changed_after,
    )

    result, exit_code = run_case(
        layout=layout,
        case_id="ID-006",
        owner="sync",
        strategy="baseline",
        run_id="case-run",
        initial_sample_id="initial-sample",
        duplicate_sample_id="duplicate-sample",
    )

    assert exit_code == cli_support.EXIT_GATE_FAILED
    assert result["observed_outcome"] == "state_changed"


def test_duplicate_completion_case_reuses_replay_noop_harness(tmp_path: Path) -> None:
    layout = benchmark_layout(tmp_path)
    initial_after = build_case_snapshot(populated=True)
    write_case_sample(
        layout,
        sample_id="initial-sample",
        strategy="baseline",
        mode="cold",
        after=initial_after,
    )
    write_case_sample(
        layout,
        sample_id="duplicate-sample",
        strategy="baseline",
        mode="warm",
        before=initial_after,
        after=initial_after,
    )

    result, exit_code = run_case(
        layout=layout,
        case_id="ID-001",
        owner="sync",
        strategy="baseline",
        run_id="case-run",
        initial_sample_id="initial-sample",
        duplicate_sample_id="duplicate-sample",
    )

    assert exit_code == cli_support.EXIT_OK
    assert result["observed_outcome"] == "no_op"
    assert result["expected_outcome"] == (
        "duplicate completion after a successful commit is idempotent"
    )


def test_replacement_case_requires_new_active_revision_and_manifest(tmp_path: Path) -> None:
    layout = benchmark_layout(tmp_path)
    initial_after = build_case_snapshot(populated=True)
    initial_after["documents"][0]["revision_ref"] = "ref-old-revision"
    initial_after["serving_revision_manifests"] = [
        {"revision_ref": "ref-old-revision"}
    ]
    initial_after["namespace_generation"] = 1
    write_case_sample(
        layout,
        sample_id="initial-sample",
        strategy="baseline",
        mode="cold",
        after=initial_after,
    )
    replacement_after = build_case_snapshot(populated=True)
    replacement_after["documents"][0]["revision_ref"] = "ref-new-revision"
    replacement_after["serving_revision_manifests"] = [
        {"revision_ref": "ref-old-revision"},
        {"revision_ref": "ref-new-revision"},
    ]
    replacement_after["namespace_generation"] = 2
    write_case_sample(
        layout,
        sample_id="replacement-sample",
        strategy="baseline",
        mode="warm",
        before=initial_after,
        after=replacement_after,
    )
    write_case_sample(
        layout,
        sample_id="candidate-initial-sample",
        strategy="candidate",
        mode="cold",
        after={
            **deepcopy(initial_after),
            "documents": [
                {
                    "document_ref": "ref-candidate-document",
                    "revision_ref": "ref-candidate-revision",
                }
            ],
            "serving_revision_manifests": [
                {"revision_ref": "ref-candidate-revision"}
            ],
        },
    )
    candidate_initial_after = {
        **deepcopy(initial_after),
        "documents": [
            {
                "document_ref": "ref-candidate-document",
                "revision_ref": "ref-candidate-revision",
            }
        ],
        "serving_revision_manifests": [
            {"revision_ref": "ref-candidate-revision"}
        ],
    }
    candidate_replacement_after = {
        **deepcopy(replacement_after),
        "documents": [
            {
                "document_ref": "ref-candidate-document",
                "revision_ref": "ref-candidate-new-revision",
            }
        ],
        "serving_revision_manifests": [
            {"revision_ref": "ref-candidate-revision"},
            {"revision_ref": "ref-candidate-new-revision"},
        ],
    }
    write_case_sample(
        layout,
        sample_id="candidate-replacement-sample",
        strategy="candidate",
        mode="warm",
        before=candidate_initial_after,
        after=candidate_replacement_after,
    )

    result, exit_code = run_case(
        layout=layout,
        case_id="ST-002",
        owner="sync",
        strategy="baseline",
        run_id="case-run",
        baseline_initial_sample_id="initial-sample",
        baseline_replacement_sample_id="replacement-sample",
        candidate_initial_sample_id="candidate-initial-sample",
        candidate_replacement_sample_id="candidate-replacement-sample",
    )

    assert exit_code == cli_support.EXIT_OK
    assert result["observed_outcome"] == "parity"
    assert result["passed"] is True


def test_replacement_case_rejects_same_active_revision(tmp_path: Path) -> None:
    layout = benchmark_layout(tmp_path)
    initial_after = build_case_snapshot(populated=True)
    write_case_sample(
        layout,
        sample_id="initial-sample",
        strategy="baseline",
        mode="cold",
        after=initial_after,
    )
    write_case_sample(
        layout,
        sample_id="replacement-sample",
        strategy="baseline",
        mode="warm",
        before=initial_after,
        after=initial_after,
    )
    write_case_sample(
        layout,
        sample_id="candidate-initial-sample",
        strategy="candidate",
        mode="cold",
        after=initial_after,
    )
    write_case_sample(
        layout,
        sample_id="candidate-replacement-sample",
        strategy="candidate",
        mode="warm",
        before=initial_after,
        after=initial_after,
    )

    result, exit_code = run_case(
        layout=layout,
        case_id="ST-002",
        owner="sync",
        strategy="baseline",
        run_id="case-run",
        baseline_initial_sample_id="initial-sample",
        baseline_replacement_sample_id="replacement-sample",
        candidate_initial_sample_id="candidate-initial-sample",
        candidate_replacement_sample_id="candidate-replacement-sample",
    )

    assert exit_code == cli_support.EXIT_GATE_FAILED
    assert "active_revision" in str(result["failure_reason"])


@pytest.mark.parametrize(
    "script_name",
    (
        "freeze_input.py",
        "clone_db.py",
        "run_publication.py",
        "run_correctness.py",
        "run_capacity.py",
        "aggregate.py",
    ),
)
def test_each_command_boots_as_a_script(script_name: str) -> None:
    """Every Phase 0 command must run from the repository-root command line."""
    script_path = (
        Path(REPOSITORY_ROOT) / "scripts" / "publication_benchmark" / script_name
    )

    completed = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        cwd=str(REPOSITORY_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage" in completed.stdout.lower()
