"""Contract tests for semantic publication state parity evidence."""

# ruff: noqa: E402

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared.services.retrieval.scoring.hierarchy import ProviderToolSpace
from shared.services.retrieval.scoring.knowhere_provider import (
    KnowhereProvider,
    SectionRow,
    UnitRow,
)
from shared.services.retrieval.scoring.persisted_score_load import (
    average_idf_from_unit_dfs,
)
from shared.services.retrieval.scoring.score_units import build_score_units
from tests.support.publication_benchmark_support import ensure_benchmark_import_path

ensure_benchmark_import_path()

from scripts.publication_benchmark.state_parity import (  # noqa: E402
    SEMANTIC_COMPONENTS,
    compare_publication_state,
    write_state_parity_artifact,
)
from scripts.publication_benchmark.state_snapshot import (  # noqa: E402
    SEMANTIC_STATE_SCHEMA_VERSION,
    STATE_SNAPSHOT_SCHEMA_VERSION,
    canonical_semantic_digest,
    normalize_serving_payload,
)

FROZEN_DIGEST = canonical_semantic_digest("frozen-input")


def test_map_unit_order_uses_section_paths_across_generated_ids() -> None:
    def build_units(first_id: str, second_id: str) -> list[dict[str, Any]]:
        provider = KnowhereProvider(
            doc_id="document-id",
            sections=[
                SectionRow(first_id, None, "source.pdf / A", "A", 1, "", 0),
                SectionRow(second_id, None, "source.pdf / B", "B", 1, "", 1),
            ],
            units=[
                UnitRow("chunk-a", first_id, "text", "Alpha body", 0),
                UnitRow("chunk-b", second_id, "text", "Beta body", 1),
            ],
        )
        return build_score_units(ProviderToolSpace(provider), "document-id")

    first = build_units("sec-z", "sec-a")
    second = build_units("sec-a", "sec-z")

    assert (
        [unit["path_text"] for unit in first]
        == [unit["path_text"] for unit in second]
        == ["A", "B"]
    )
    assert [unit["content_search_text"] for unit in first] == [
        unit["content_search_text"] for unit in second
    ]


def test_map_unit_average_idf_is_independent_of_token_iteration_order() -> None:
    frequencies = {
        f"token-{index}": (index % 97) + 1 for index in range(5_000)
    }
    reversed_frequencies = dict(reversed(tuple(frequencies.items())))

    assert average_idf_from_unit_dfs(
        unit_count=100,
        token_document_frequency=frequencies,
    ) == average_idf_from_unit_dfs(
        unit_count=100,
        token_document_frequency=reversed_frequencies,
    )


def build_serving_payload(
    section_id: str,
    *,
    job_result_id: str = "stable-revision",
    job_id: str = "stable-job",
) -> dict[str, Any]:
    return {
        "document_id": "generated-document-id",
        "job_result_id": job_result_id,
        "job_id": job_id,
        "source_file_name": "benchmark-source.pdf",
        "sections": [
            {
                "section_id": section_id,
                "parent_section_id": None,
                "section_path": "benchmark-source.pdf / Overview",
                "section_title": "Overview",
                "section_level": 1,
                "summary": "Summary",
                "sort_order": 0,
            }
        ],
        "chunks": [
            {
                "chunk_id": "stable-chunk",
                "section_id": section_id,
                "chunk_type": "text",
                "sort_order": 0,
                "connect_to": [],
            }
        ],
        "root_asset_ids": [],
        "remounted_assets_by_section": {section_id: ["stable-image"]},
    }


def build_snapshot() -> dict[str, Any]:
    return {
        "schema_version": STATE_SNAPSHOT_SCHEMA_VERSION,
        "scope_ref": "scope-opaque",
        "source_file_name_refs": ["ref-filename"],
        "relation_counts": {"documents_active": 1, "document_chunks": 1},
        "chunk_type_counts": {"text": 1},
        "namespace_generation": 1,
        "namespace_snapshot": {
            "format_version": 2,
            "checksum": "random-section-id-checksum",
            "payload_bytes": 100,
        },
        "serving_revision_manifests": [
            {
                "document_ref": "random-document-ref",
                "revision_ref": "revision-ref",
                "checksum": "random-section-id-checksum",
                "payload_bytes": 200,
                "format_version": 1,
            }
        ],
        "documents": [{"document_ref": "random-document-ref", "chunk_count": 1}],
        "semantic_fingerprints": {
            "schema_version": SEMANTIC_STATE_SCHEMA_VERSION,
            **{name: canonical_semantic_digest([name]) for name in SEMANTIC_COMPONENTS},
        },
    }


def test_serving_payload_normalizes_generated_section_ids() -> None:
    first = normalize_serving_payload(
        build_serving_payload(
            "sec-random-first", job_result_id="revision-first", job_id="job-first"
        ),
        document_names={"generated-document-id": "benchmark-source.pdf"},
    )
    second = normalize_serving_payload(
        build_serving_payload(
            "sec-random-second", job_result_id="revision-second", job_id="job-second"
        ),
        document_names={"generated-document-id": "benchmark-source.pdf"},
    )

    assert canonical_semantic_digest(first) == canonical_semantic_digest(second)
    second["chunks"][0]["chunk_type"] = "table"
    assert canonical_semantic_digest(first) != canonical_semantic_digest(second)


def test_state_parity_ignores_generated_ids_and_checksums() -> None:
    disabled = build_snapshot()
    enabled = build_snapshot()
    enabled["namespace_snapshot"]["checksum"] = "another-random-checksum"
    enabled["serving_revision_manifests"][0]["checksum"] = "another-checksum"
    enabled["serving_revision_manifests"][0]["payload_bytes"] = 220
    enabled["documents"][0]["document_ref"] = "another-random-document-ref"

    result = compare_publication_state(
        disabled_snapshot=disabled,
        enabled_snapshot=enabled,
        disabled_input_digest=FROZEN_DIGEST,
        enabled_input_digest=FROZEN_DIGEST,
    )

    assert result.status == "pass"
    assert result.mismatches == ()


def test_state_parity_requires_matching_input_scope_source_and_semantics() -> None:
    disabled = build_snapshot()
    enabled = build_snapshot()
    enabled["scope_ref"] = "scope-other"
    enabled["source_file_name_refs"] = ["ref-other"]
    enabled["semantic_fingerprints"]["chunks"] = canonical_semantic_digest(
        "changed-content"
    )

    result = compare_publication_state(
        disabled_snapshot=disabled,
        enabled_snapshot=enabled,
        disabled_input_digest=canonical_semantic_digest("frozen-a"),
        enabled_input_digest=canonical_semantic_digest("frozen-b"),
    )

    assert result.status == "fail"
    assert result.mismatches == (
        "chunks",
        "frozen_input_digest",
        "scope_ref",
        "source_file_name_refs",
    )


def test_state_parity_fails_closed_without_semantic_fingerprints() -> None:
    disabled = build_snapshot()
    enabled = build_snapshot()
    del enabled["semantic_fingerprints"]

    result = compare_publication_state(
        disabled_snapshot=disabled,
        enabled_snapshot=enabled,
        disabled_input_digest=FROZEN_DIGEST,
        enabled_input_digest=FROZEN_DIGEST,
    )

    assert result.status == "fail"
    assert "enabled.semantic_schema" in result.mismatches


def test_state_parity_artifact_contains_only_opaque_values(tmp_path: Path) -> None:
    result = compare_publication_state(
        disabled_snapshot=build_snapshot(),
        enabled_snapshot=build_snapshot(),
        disabled_input_digest=FROZEN_DIGEST,
        enabled_input_digest=FROZEN_DIGEST,
    )
    artifact_path = tmp_path / "state-parity.json"

    write_state_parity_artifact(
        path=artifact_path,
        result=result,
        sample_refs=["raw-sample-identifier"],
    )

    serialized = artifact_path.read_text(encoding="utf-8")
    artifact = json.loads(serialized)
    assert artifact["status"] == "pass"
    assert artifact["sample_refs"][0].startswith("ref-")
    assert "raw-sample-identifier" not in serialized
