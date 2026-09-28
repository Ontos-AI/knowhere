"""Verification case registry for the Publication benchmark.

Case identifiers are part of the report contract. The registry is compared
against the case list in the design document. Unimplemented cases fail loudly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from scripts.publication_benchmark.run_record import (
    PublicationRunRecord,
    find_comparability_conflicts,
)
from scripts.publication_benchmark.state_parity import compare_publication_state
from scripts.publication_benchmark.state_snapshot import STATE_SNAPSHOT_SCHEMA_VERSION

PLAN_DOCUMENT_RELATIVE_PATH: str = (
    "docs/design/publication-performance-optimization-plan.md"
)

_CASE_LINE_PATTERN = re.compile(
    r"^(?P<start>[A-Z]{2}-\d{3})"
    r"(?:\.\.(?P<end>[A-Z]{2}-\d{3}))?"
    r"\s{2,}(?P<title>.+)$"
)

_CASE_TITLES: tuple[tuple[str, str], ...] = (
    ("LP-001", "lifecycle parity: new document"),
    ("LP-002", "lifecycle parity: replacement revision"),
    ("SI-001", "two users x two namespaces isolation"),
    ("ST-001", "state parity: new document"),
    ("ST-002", "state parity: replacement revision"),
    ("AT-001", "failure after sections and chunks stage"),
    ("AT-002", "failure after map-unit persistence"),
    ("AT-003", "failure after token persistence"),
    ("AT-004", "failure after serving manifest persistence"),
    ("AT-005", "failure after graph publication"),
    ("AT-006", "failure after namespace snapshot persistence"),
    ("AT-007", "failure immediately before commit"),
    ("AT-008", "failure before commit on replacement revision"),
    ("RC-001", "connection loss before commit"),
    ("RC-002", "connection loss during commit"),
    ("RC-003", "process interruption after commit before effects"),
    ("RC-004", "unknown commit outcome retry/reconciliation"),
    ("ID-001", "duplicate completion after successful commit"),
    ("ID-002", "concurrent replacement in opposite completion order"),
    ("ID-003", "stale completion after newer revision"),
    ("ID-004", "namespace movement"),
    ("ID-005", "archived-document publication"),
    ("ID-006", "all-duplicate input no-op"),
    ("SC-001", "baseline reads baseline state"),
    ("SC-002", "baseline reads candidate state"),
    ("SC-003", "candidate reads baseline state"),
    ("SC-004", "candidate reads candidate state"),
    ("PE-001", "rollback has no cache invalidation or webhook enqueue"),
    ("PE-002", "successful effects occur once"),
    ("PE-003", "post-commit effect failure recovery"),
)


class CaseNotImplementedError(RuntimeError):
    """Raised when a verification case has no harness yet."""

    def __init__(self, case_id: str, title: str) -> None:
        super().__init__(
            f"verification case {case_id} ({title}) has no implementation yet; "
            "Phase 3 must implement its harness before candidate evidence exists"
        )
        self.case_id = case_id
        self.title = title


@dataclass(frozen=True)
class VerificationCase:
    """One verification case identifier and its title."""

    case_id: str
    title: str

    def to_dict(self) -> dict[str, str]:
        return {"case_id": self.case_id, "title": self.title}


@dataclass(frozen=True)
class CaseResult:
    """Normalized result of one verification case run."""

    case_id: str
    title: str
    expected_outcome: str
    observed_outcome: str
    passed: bool
    failure_reason: str | None
    state_before: Mapping[str, object] | None = None
    state_after: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "expected_outcome": self.expected_outcome,
            "observed_outcome": self.observed_outcome,
            "passed": self.passed,
            "failure_reason": self.failure_reason,
            "state_before": dict(self.state_before or {}),
            "state_after": dict(self.state_after or {}),
        }


@dataclass(frozen=True)
class PublicationCaseEvidence:
    """One committed sample and its recorded publication state."""

    record: PublicationRunRecord
    state_before: Mapping[str, Any]
    state_after: Mapping[str, Any]


@dataclass(frozen=True)
class ReplacementCaseEvidence:
    """One cold publication followed by a replacement on the same clone."""

    initial: PublicationCaseEvidence
    replacement: PublicationCaseEvidence


CASE_REGISTRY: Mapping[str, VerificationCase] = {
    case_id: VerificationCase(case_id=case_id, title=title)
    for case_id, title in _CASE_TITLES
}

IMPLEMENTED_CASE_IDS: frozenset[str] = frozenset(
    {"ST-001", "ST-002", "ID-001", "ID-006"}
)


def documented_case_ids(plan_path: Path) -> tuple[str, ...]:
    """Return the case identifiers listed in the design document."""
    if not plan_path.is_file():
        raise FileNotFoundError(f"publication plan document not found: {plan_path}")
    return parse_case_ids(plan_path.read_text(encoding="utf-8"))


def parse_case_ids(plan_text: str) -> tuple[str, ...]:
    """Parse the case identifier block of the design document.

    Any fenced text block may carry case lines; the block that lists case
    identifiers is the one whose lines match the ``<PREFIX>-<NNN>`` shape.
    """
    case_ids: list[str] = []
    for block in _fenced_blocks(plan_text):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        for line in lines:
            match = _CASE_LINE_PATTERN.match(line)
            if match is None:
                continue
            start = match.group("start")
            end = match.group("end")
            if end is None:
                case_ids.append(start)
                continue
            case_ids.extend(_expand_case_range(start, end))
    return tuple(case_ids)


def _fenced_blocks(text: str) -> tuple[str, ...]:
    blocks: list[str] = []
    for match in re.finditer(
        r"```(?P<language>[A-Za-z0-9_-]*)[ \t]*\n(?P<body>.*?)```",
        text,
        flags=re.DOTALL,
    ):
        if match.group("language").lower() in ("", "text"):
            blocks.append(match.group("body"))
    return tuple(blocks)


def _expand_case_range(start: str, end: str) -> tuple[str, ...]:
    start_prefix, start_index = start.split("-")
    end_prefix, end_index = end.split("-")
    if start_prefix != end_prefix:
        raise ValueError(f"case range {start}..{end} mixes prefixes")
    return tuple(
        f"{start_prefix}-{index:03d}"
        for index in range(int(start_index), int(end_index) + 1)
    )


def find_registry_mismatches(plan_path: Path) -> tuple[str, ...]:
    """Return case identifiers that differ between document and registry."""
    documented = documented_case_ids(plan_path)
    registered = tuple(CASE_REGISTRY)
    mismatches = [
        f"document-only:{case_id}"
        for case_id in documented
        if case_id not in CASE_REGISTRY
    ]
    mismatches.extend(
        f"registry-only:{case_id}"
        for case_id in registered
        if case_id not in documented
    )
    return tuple(mismatches)


def require_case(case_id: str) -> VerificationCase:
    """Return one registered case or raise."""
    case = CASE_REGISTRY.get(case_id)
    if case is None:
        raise KeyError(
            f"unknown verification case {case_id!r}; documented cases are "
            f"{list(CASE_REGISTRY)}"
        )
    return case


def unavailable_case_result(case_id: str) -> CaseResult:
    """Return the loud placeholder result for an unimplemented case."""
    case = require_case(case_id)
    if case_id in IMPLEMENTED_CASE_IDS:
        raise ValueError(f"{case_id} has an implemented correctness harness")
    return CaseResult(
        case_id=case.case_id,
        title=case.title,
        expected_outcome="case harness defined by Phase 3 of the plan",
        observed_outcome="not_implemented",
        passed=False,
        failure_reason=(
            "case harness is not implemented in Phase 0; this placeholder "
            "fails loudly instead of silently skipping"
        ),
    )


def run_verification_case(
    case_id: str,
    *,
    baseline: PublicationCaseEvidence | None = None,
    candidate: PublicationCaseEvidence | None = None,
    initial: PublicationCaseEvidence | None = None,
    duplicate: PublicationCaseEvidence | None = None,
    baseline_replacement: ReplacementCaseEvidence | None = None,
    candidate_replacement: ReplacementCaseEvidence | None = None,
) -> CaseResult:
    """Run an implemented case or fail loudly for an unimplemented case."""
    case = require_case(case_id)
    if case_id == "ST-001":
        return _verify_new_document_state(case, baseline, candidate)
    if case_id == "ST-002":
        return _verify_replacement_state(case, baseline_replacement, candidate_replacement)
    if case_id in ("ID-001", "ID-006"):
        return _verify_replay_noop(case, initial, duplicate)
    raise CaseNotImplementedError(case.case_id, case.title)


def _verify_replacement_state(
    case: VerificationCase,
    baseline: ReplacementCaseEvidence | None,
    candidate: ReplacementCaseEvidence | None,
) -> CaseResult:
    expected = "baseline and candidate replacement revisions have identical persisted state"
    if baseline is None or candidate is None:
        return CaseResult(
            case_id=case.case_id,
            title=case.title,
            expected_outcome=expected,
            observed_outcome="missing_evidence",
            passed=False,
            failure_reason=(
                "ST-002 requires baseline and candidate cold-to-replacement pairs"
            ),
        )
    mismatches: list[str] = []
    for label, pair, strategy in (
        ("baseline", baseline, "baseline"),
        ("candidate", candidate, "candidate"),
    ):
        mismatches.extend(
            f"{label}.{field}" for field in _replacement_transition_mismatches(pair)
        )
        for phase in (pair.initial, pair.replacement):
            if phase.record.strategy != strategy:
                mismatches.append(f"{label}.strategy")
    for phase in ("initial", "replacement"):
        baseline_sample = getattr(baseline, phase)
        candidate_sample = getattr(candidate, phase)
        mismatches.extend(
            f"{phase}.run_record.{field}"
            for field in find_comparability_conflicts(
                baseline_sample.record, candidate_sample.record
            )
        )
        parity = compare_publication_state(
            disabled_snapshot=baseline_sample.state_after,
            enabled_snapshot=candidate_sample.state_after,
            disabled_input_digest=baseline_sample.record.input_digest,
            enabled_input_digest=candidate_sample.record.input_digest,
        )
        mismatches.extend(f"{phase}.state.{field}" for field in parity.mismatches)
    if baseline.initial.state_before != candidate.initial.state_before:
        mismatches.append("initial.before")
    # The replacement sample's before snapshot is the initial sample's after
    # snapshot for that strategy. Compare those snapshots through the semantic
    # parity checks above because generated document and revision references may
    # differ between isolated clones.
    return CaseResult(
        case_id=case.case_id,
        title=case.title,
        expected_outcome=expected,
        observed_outcome="parity" if not mismatches else "mismatch",
        passed=not mismatches,
        failure_reason=", ".join(sorted(set(mismatches))) if mismatches else None,
        state_before={
            "baseline_initial": dict(baseline.initial.state_before),
            "baseline_replacement": dict(baseline.replacement.state_before),
            "candidate_initial": dict(candidate.initial.state_before),
            "candidate_replacement": dict(candidate.replacement.state_before),
        },
        state_after={
            "baseline_initial": dict(baseline.initial.state_after),
            "baseline_replacement": dict(baseline.replacement.state_after),
            "candidate_initial": dict(candidate.initial.state_after),
            "candidate_replacement": dict(candidate.replacement.state_after),
        },
    )


def _replacement_transition_mismatches(pair: ReplacementCaseEvidence) -> tuple[str, ...]:
    initial = pair.initial
    replacement = pair.replacement
    mismatches: list[str] = []
    if initial.record.mode != "cold":
        mismatches.append("initial.mode")
    if replacement.record.mode != "warm":
        mismatches.append("replacement.mode")
    for label, evidence in (("initial", initial), ("replacement", replacement)):
        if evidence.record.outcome != "committed":
            mismatches.append(f"{label}.outcome")
        if evidence.record.run_id != initial.record.run_id:
            mismatches.append(f"{label}.run_id")
        if evidence.record.clone_id != initial.record.clone_id:
            mismatches.append(f"{label}.clone_id")
    for field in (
        "input_digest", "template_content_digest", "source_content_digest", "dependency_lock_digest",
        "postgres_profile", "postgres_version", "postgres_settings_digest",
        "clone_source_digest", "host_id", "redis_namespace", "owner", "strategy",
    ):
        if getattr(initial.record, field) != getattr(replacement.record, field):
            mismatches.append(f"run_record.{field}")
    if not initial.record.template_content_digest:
        mismatches.append("template_content_digest")
    before = replacement.state_before
    initial_after = initial.state_after
    after = replacement.state_after
    if initial_after != before:
        mismatches.append("replacement.before")
    if not _is_populated_state(initial_after):
        mismatches.append("initial.after_not_populated")
    if not _is_populated_state(after):
        mismatches.append("replacement.after_not_populated")
    if initial_after.get("scope_ref") != after.get("scope_ref"):
        mismatches.append("scope_ref")
    if initial_after.get("source_file_name_refs") != after.get("source_file_name_refs"):
        mismatches.append("source_file_name_refs")
    if _relation_counts(initial_after).get("documents_active") != 1:
        mismatches.append("initial.documents_active")
    if _relation_counts(after).get("documents_active") != 1:
        mismatches.append("replacement.documents_active")
    if _relation_counts(after).get("documents_archived") != 0:
        mismatches.append("replacement.documents_archived")
    if _manifest_count(after) != _manifest_count(initial_after) + 1:
        mismatches.append("serving_revision_manifests")
    if _namespace_generation(after) <= _namespace_generation(initial_after):
        mismatches.append("namespace_generation")
    old_revision = _active_revision_ref(initial_after)
    new_revision = _active_revision_ref(after)
    if not old_revision or not new_revision or old_revision == new_revision:
        mismatches.append("active_revision")
    return tuple(mismatches)


def _relation_counts(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    counts = snapshot.get("relation_counts")
    return counts if isinstance(counts, Mapping) else {}


def _manifest_count(snapshot: Mapping[str, Any]) -> int:
    manifests = snapshot.get("serving_revision_manifests")
    return len(manifests) if isinstance(manifests, list) else 0


def _namespace_generation(snapshot: Mapping[str, Any]) -> int:
    value = snapshot.get("namespace_generation")
    return int(value) if isinstance(value, int) else -1


def _active_revision_ref(snapshot: Mapping[str, Any]) -> object:
    documents = snapshot.get("documents")
    if not isinstance(documents, list) or len(documents) != 1:
        return None
    document = documents[0]
    return document.get("revision_ref") if isinstance(document, Mapping) else None


def _verify_replay_noop(
    case: VerificationCase,
    initial: PublicationCaseEvidence | None,
    duplicate: PublicationCaseEvidence | None,
) -> CaseResult:
    expected = (
        "duplicate completion after a successful commit is idempotent"
        if case.case_id == "ID-001"
        else "all-duplicate input commits as a no-op without changing state"
    )
    if initial is None or duplicate is None:
        return CaseResult(
            case_id=case.case_id,
            title=case.title,
            expected_outcome=expected,
            observed_outcome="missing_evidence",
            passed=False,
            failure_reason=(
                f"{case.case_id} requires an initial cold sample and a duplicate warm sample"
            ),
        )

    mismatches: list[str] = []
    if initial.record.mode != "cold":
        mismatches.append("initial.mode")
    if duplicate.record.mode != "warm":
        mismatches.append("duplicate.mode")
    for label, evidence in (("initial", initial), ("duplicate", duplicate)):
        if evidence.record.outcome != "committed":
            mismatches.append(f"{label}.outcome")
        if evidence.record.run_id != initial.record.run_id:
            mismatches.append(f"{label}.run_id")
        if evidence.record.clone_id != initial.record.clone_id:
            mismatches.append(f"{label}.clone_id")
    for field in (
        "input_digest",
        "template_content_digest",
        "source_content_digest",
        "dependency_lock_digest",
        "postgres_profile",
        "postgres_version",
        "postgres_settings_digest",
        "clone_source_digest",
        "host_id",
        "redis_namespace",
        "owner",
        "strategy",
    ):
        if getattr(initial.record, field) != getattr(duplicate.record, field):
            mismatches.append(f"run_record.{field}")

    initial_after = initial.state_after
    duplicate_before = duplicate.state_before
    duplicate_after = duplicate.state_after
    if not _is_populated_state(initial_after):
        mismatches.append("initial.after_not_populated")
    if initial_after != duplicate_before:
        mismatches.append("duplicate.before")
    if duplicate_before != duplicate_after:
        mismatches.append("duplicate.after_changed")
    parity = compare_publication_state(
        disabled_snapshot=duplicate_before,
        enabled_snapshot=duplicate_after,
        disabled_input_digest=initial.record.input_digest,
        enabled_input_digest=duplicate.record.input_digest,
    )
    mismatches.extend(f"state.{field}" for field in parity.mismatches)
    return CaseResult(
        case_id=case.case_id,
        title=case.title,
        expected_outcome=expected,
        observed_outcome="no_op" if not mismatches else "state_changed",
        passed=not mismatches,
        failure_reason=", ".join(sorted(set(mismatches))) if mismatches else None,
        state_before={
            "initial": dict(initial.state_before),
            "duplicate": dict(duplicate.state_before),
        },
        state_after={
            "initial": dict(initial_after),
            "duplicate": dict(duplicate_after),
        },
    )


def _is_populated_state(snapshot: Mapping[str, Any]) -> bool:
    counts = snapshot.get("relation_counts")
    if not isinstance(counts, Mapping):
        return False
    return (
        isinstance(counts.get("documents_active"), int)
        and int(counts["documents_active"]) >= 1
        and isinstance(snapshot.get("semantic_fingerprints"), Mapping)
    )


def _verify_new_document_state(
    case: VerificationCase,
    baseline: PublicationCaseEvidence | None,
    candidate: PublicationCaseEvidence | None,
) -> CaseResult:
    expected = "comparable cold publications create identical new-document state"
    if baseline is None or candidate is None:
        return CaseResult(
            case_id=case.case_id,
            title=case.title,
            expected_outcome=expected,
            observed_outcome="missing_evidence",
            passed=False,
            failure_reason="ST-001 requires baseline and candidate sample IDs",
        )

    mismatches: list[str] = []
    if baseline.record.strategy != "baseline":
        mismatches.append("baseline.strategy")
    if candidate.record.strategy != "candidate":
        mismatches.append("candidate.strategy")
    if baseline.record.run_id != candidate.record.run_id:
        mismatches.append("run_id")
    if baseline.record.sample_id == candidate.record.sample_id:
        mismatches.append("sample_id")
    if baseline.record.template_content_digest is None:
        mismatches.append("baseline.template_content_digest")
    if candidate.record.template_content_digest is None:
        mismatches.append("candidate.template_content_digest")
    if baseline.state_before.get("semantic_fingerprints") != candidate.state_before.get(
        "semantic_fingerprints"
    ):
        mismatches.append("before.semantic_fingerprints")
    for label, evidence in (("baseline", baseline), ("candidate", candidate)):
        if evidence.state_before.get("scope_ref") != evidence.state_after.get(
            "scope_ref"
        ):
            mismatches.append(f"{label}.scope_ref")
        if evidence.record.mode != "cold":
            mismatches.append(f"{label}.mode")
        if evidence.record.outcome != "committed":
            mismatches.append(f"{label}.outcome")
        mismatches.extend(
            f"{label}.before.{field}"
            for field in _new_document_before_mismatches(evidence.state_before)
        )
        mismatches.extend(
            f"{label}.after.{field}"
            for field in _new_document_after_mismatches(evidence.state_after)
        )
    mismatches.extend(
        f"run_record.{field}"
        for field in find_comparability_conflicts(baseline.record, candidate.record)
    )
    parity = compare_publication_state(
        disabled_snapshot=baseline.state_after,
        enabled_snapshot=candidate.state_after,
        disabled_input_digest=baseline.record.input_digest,
        enabled_input_digest=candidate.record.input_digest,
    )
    mismatches.extend(f"state.{field}" for field in parity.mismatches)
    return CaseResult(
        case_id=case.case_id,
        title=case.title,
        expected_outcome=expected,
        observed_outcome="parity" if not mismatches else "mismatch",
        passed=not mismatches,
        failure_reason=", ".join(sorted(set(mismatches))) if mismatches else None,
        state_before={
            "baseline": dict(baseline.state_before),
            "candidate": dict(candidate.state_before),
        },
        state_after={
            "baseline": dict(baseline.state_after),
            "candidate": dict(candidate.state_after),
        },
    )


def _new_document_before_mismatches(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    mismatches: list[str] = []
    if snapshot.get("schema_version") != STATE_SNAPSHOT_SCHEMA_VERSION:
        mismatches.append("schema_version")
    if not snapshot.get("scope_ref"):
        mismatches.append("scope_ref")
    counts = snapshot.get("relation_counts")
    if not isinstance(counts, Mapping) or not counts:
        mismatches.append("relation_counts")
    elif any(not isinstance(count, int) or count != 0 for count in counts.values()):
        mismatches.append("nonempty_relations")
    for field in (
        "documents",
        "serving_revision_manifests",
        "source_file_name_refs",
    ):
        if snapshot.get(field) != []:
            mismatches.append(field)
    if snapshot.get("namespace_snapshot") is not None:
        mismatches.append("namespace_snapshot")
    if snapshot.get("namespace_generation") is not None:
        mismatches.append("namespace_generation")
    return tuple(mismatches)


def _new_document_after_mismatches(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    mismatches: list[str] = []
    if snapshot.get("schema_version") != STATE_SNAPSHOT_SCHEMA_VERSION:
        mismatches.append("schema_version")
    counts = snapshot.get("relation_counts")
    if not isinstance(counts, Mapping):
        return ("relation_counts",)
    for field in (
        "documents_active",
        "document_chunks",
        "document_map_units",
        "document_map_unit_tokens",
        "document_map_unit_indexes",
        "graph_nodes",
    ):
        count = counts.get(field)
        if not isinstance(count, int) or count < 1:
            mismatches.append(field)
    for field in ("documents", "serving_revision_manifests", "source_file_name_refs"):
        value = snapshot.get(field)
        if not isinstance(value, list) or len(value) != 1:
            mismatches.append(field)
    if snapshot.get("namespace_snapshot") is None:
        mismatches.append("namespace_snapshot")
    generation = snapshot.get("namespace_generation")
    if not isinstance(generation, int) or generation < 1:
        mismatches.append("namespace_generation")
    return tuple(mismatches)
