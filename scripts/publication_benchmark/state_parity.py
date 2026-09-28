"""Safe semantic parity check for traced and untraced publication samples.

The benchmark captures snapshots after the measured transaction. Random
database IDs and compressed payload bytes vary across fresh clones, so this
comparison uses fingerprints of publication-owned semantic rows instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.publication_benchmark import cli_support
from scripts.publication_benchmark.report import assert_report_is_redacted
from scripts.publication_benchmark.state_snapshot import (
    SEMANTIC_STATE_SCHEMA_VERSION,
    STATE_SNAPSHOT_SCHEMA_VERSION,
    opaque_reference,
)

STATE_PARITY_SCHEMA_VERSION: str = "publication-state-parity/1"
SEMANTIC_COMPONENTS: tuple[str, ...] = (
    "documents",
    "sections",
    "chunks",
    "map_units",
    "map_unit_tokens",
    "map_unit_indexes",
    "graph_nodes",
    "graph_edges",
    "serving_manifests",
    "namespace_snapshots",
)
_DIGEST_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")


@dataclass(frozen=True)
class StateParityResult:
    """A redaction-safe comparison of two completed publication snapshots."""

    status: str
    mismatches: tuple[str, ...]
    input_digest: str | None
    component_count: int
    disabled_fingerprints: Mapping[str, str]
    enabled_fingerprints: Mapping[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": STATE_PARITY_SCHEMA_VERSION,
            "status": self.status,
            "mismatches": list(self.mismatches),
            "input_digest": self.input_digest,
            "component_count": self.component_count,
            "disabled_fingerprints": dict(self.disabled_fingerprints),
            "enabled_fingerprints": dict(self.enabled_fingerprints),
            "comparison_scope": (
                "publication-owned persisted rows, serving manifest, and "
                "namespace snapshot; generated IDs and timestamps excluded"
            ),
        }


def compare_publication_state(
    *,
    disabled_snapshot: Mapping[str, Any],
    enabled_snapshot: Mapping[str, Any],
    disabled_input_digest: str,
    enabled_input_digest: str,
) -> StateParityResult:
    """Compare like-for-like states without exposing source data.

    The caller must use the same frozen input, synthetic scope, source filename,
    owner, strategy, and database profile. This function checks the first three
    from persisted evidence; run-record comparability checks the rest.
    """
    mismatches: list[str] = []
    matching_digest = (
        disabled_input_digest
        if _DIGEST_PATTERN.fullmatch(disabled_input_digest)
        and disabled_input_digest == enabled_input_digest
        else None
    )
    if matching_digest is None:
        mismatches.append("frozen_input_digest")
    for label, snapshot in (
        ("disabled", disabled_snapshot),
        ("enabled", enabled_snapshot),
    ):
        if snapshot.get("schema_version") != STATE_SNAPSHOT_SCHEMA_VERSION:
            mismatches.append(f"{label}.snapshot_schema")
        semantic = snapshot.get("semantic_fingerprints")
        if not isinstance(semantic, Mapping) or semantic.get("schema_version") != (
            SEMANTIC_STATE_SCHEMA_VERSION
        ):
            mismatches.append(f"{label}.semantic_schema")
            continue
        for component in SEMANTIC_COMPONENTS:
            digest = semantic.get(component)
            if not isinstance(digest, str) or not _DIGEST_PATTERN.fullmatch(digest):
                mismatches.append(f"{label}.{component}.missing_digest")
    for field in (
        "scope_ref",
        "source_file_name_refs",
        "relation_counts",
        "chunk_type_counts",
        "namespace_generation",
    ):
        if field in ("scope_ref", "source_file_name_refs") and (
            not disabled_snapshot.get(field) or not enabled_snapshot.get(field)
        ):
            mismatches.append(f"{field}.missing")
        if disabled_snapshot.get(field) != enabled_snapshot.get(field):
            mismatches.append(field)
    disabled_semantic = disabled_snapshot.get("semantic_fingerprints")
    enabled_semantic = enabled_snapshot.get("semantic_fingerprints")
    if isinstance(disabled_semantic, Mapping) and isinstance(enabled_semantic, Mapping):
        for component in SEMANTIC_COMPONENTS:
            if disabled_semantic.get(component) != enabled_semantic.get(component):
                mismatches.append(component)
    return StateParityResult(
        status="pass" if not mismatches else "fail",
        mismatches=tuple(sorted(set(mismatches))),
        input_digest=matching_digest,
        component_count=len(SEMANTIC_COMPONENTS),
        disabled_fingerprints={
            component: str(disabled_semantic.get(component))
            for component in SEMANTIC_COMPONENTS
            if isinstance(disabled_semantic.get(component), str)
            and _DIGEST_PATTERN.fullmatch(str(disabled_semantic.get(component)))
        }
        if isinstance(disabled_semantic, Mapping)
        else {},
        enabled_fingerprints={
            component: str(enabled_semantic.get(component))
            for component in SEMANTIC_COMPONENTS
            if isinstance(enabled_semantic.get(component), str)
            and _DIGEST_PATTERN.fullmatch(str(enabled_semantic.get(component)))
        }
        if isinstance(enabled_semantic, Mapping)
        else {},
    )


def write_state_parity_artifact(
    *,
    path: Path,
    result: StateParityResult,
    sample_refs: Sequence[str],
) -> None:
    """Write only opaque sample references and comparison results."""
    artifact = {
        **result.to_dict(),
        "sample_refs": [opaque_reference(sample) for sample in sample_refs],
    }
    assert_report_is_redacted(artifact)
    cli_support.write_json(path, artifact)
