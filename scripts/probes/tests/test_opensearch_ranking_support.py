"""Pure ranking gates and manifest validation; no database or native node needed."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

import pytest
from pydantic import ValidationError


def loadSupport() -> type:
    modulePath: Path = Path(__file__).parents[1] / "opensearch-ranking-support.py"
    assert modulePath.is_file(), "The relative ranking evaluator is not implemented"
    specification = importlib.util.spec_from_file_location("ranking_support", modulePath)
    assert specification is not None and specification.loader is not None
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return cast(type, module.ReplaySupport)


def createReport(
    postgresql: list[int | None],
    opensearch: list[int | None],
    *,
    identityFailures: list[str] | None = None,
) -> dict[str, object]:
    return {
        "stages": [
            {
                "stage": "before-staging",
                "namespace": "private",
                "queries": [
                    {
                        "id": "query-one",
                        "top_k": 10,
                        "evidence_groups": [
                            [{"document": "document-one", "chunk_id": f"chunk-{index}"}]
                            for index in range(len(postgresql))
                        ],
                        "ranks": {"postgresql": postgresql, "opensearch": opensearch},
                        "identity_failures": identityFailures or [],
                    }
                ],
            }
        ]
    }


def createManifest(count: int = 20) -> dict[str, object]:
    return {
        "provenance": "business_reviewed",
        "reviewer": "Named fixture reviewer",
        "documents": [
            {
                "key": "document-one",
                "namespace": "private",
                "file_name": "Manual",
                "chunks_path": "chunks.json",
                "replace": True,
            },
            {
                "key": "document-two",
                "namespace": "other-private",
                "file_name": "Other manual",
                "chunks_path": "other-chunks.json",
                "replace": True,
            },
        ],
        "queries": [
            {
                "id": f"query-{index}",
                "namespace": "private",
                "text": f"useful query {index}",
                "top_k": 10,
                "expected": [[{"document": "document-one", "chunk_id": "chunk-one"}]],
            }
            for index in range(count)
        ],
    }


def extendManifest(manifest: dict[str, object]) -> dict[str, object]:
    return {
        **manifest,
        "suite": {
            "kind": "extended",
            "id": "reviewed-extended",
            "version": "1",
            "baseline_manifest_sha256": "a" * 64,
        },
    }


def test_fails_each_lost_pg_found_group_even_when_another_group_is_retained() -> None:
    support: type = loadSupport()
    result: dict[str, object] = support.evaluateReport(createReport([1, 3], [2, None]))
    assert result["passed"] is False
    assert result["counts"] == {
        "shared_success": 1,
        "postgresql_only": 1,
        "opensearch_only": 0,
        "shared_miss": 0,
    }
    groups: list[dict[str, object]] = cast(list[dict[str, object]], result["groups"])
    assert groups[1]["evidence"] == [{"document": "document-one", "chunk_id": "chunk-1"}]
    assert groups[1]["postgresql_rank"] == 3
    assert groups[1]["opensearch_rank"] is None


def test_reports_shared_misses_and_native_only_without_failing_relative_gate() -> None:
    support: type = loadSupport()
    result: dict[str, object] = support.evaluateReport(createReport([None, None], [None, 4]))
    assert result["passed"] is True
    assert result["counts"] == {
        "shared_success": 0,
        "postgresql_only": 0,
        "opensearch_only": 1,
        "shared_miss": 1,
    }


@pytest.mark.parametrize("ranks", [([1], [1]), ([None], [None])])
def test_identity_failure_independently_fails_even_if_pg_has_the_same_defect(
    ranks: tuple[list[int | None], list[int | None]],
) -> None:
    support: type = loadSupport()
    result: dict[str, object] = support.evaluateReport(
        createReport(*ranks, identityFailures=["opensearch: wrong source revision"])
    )
    assert result["passed"] is False
    assert result["identity_failures"] == [
        {
            "stage": "before-staging",
            "namespace": "private",
            "query_id": "query-one",
            "reason": "opensearch: wrong source revision",
        }
    ]


@pytest.mark.parametrize("opensearch", [[], [1, 2], [0], [11]])
def test_rejects_missing_extra_and_out_of_range_rank_observations(
    opensearch: list[int | None],
) -> None:
    support: type = loadSupport()
    with pytest.raises(ValueError):
        support.evaluateReport(createReport([1], opensearch))


def test_preserves_exactly_twenty_legacy_baseline_cases() -> None:
    support: type = loadSupport()
    manifest = support.parseManifest(createManifest())
    assert manifest.suite.kind == "baseline"
    assert manifest.suite.id == "original-20"
    assert manifest.suite.version == "1"
    with pytest.raises(ValidationError, match="exactly 20"):
        support.parseManifest(createManifest(21))


@pytest.mark.parametrize("count", [1, 3, 21, 37])
def test_accepts_separately_versioned_reviewed_extended_query_counts(count: int) -> None:
    support: type = loadSupport()
    manifest = support.parseManifest(extendManifest(createManifest(count)))
    assert manifest.suite.kind == "extended"
    assert len(manifest.queries) == count


def test_rejects_duplicate_extended_ids_and_missing_baseline_digest() -> None:
    support: type = loadSupport()
    manifest: dict[str, object] = extendManifest(createManifest(2))
    queries: list[dict[str, object]] = cast(list[dict[str, object]], manifest["queries"])
    queries[1]["id"] = queries[0]["id"]
    with pytest.raises(ValidationError, match="unique query IDs"):
        support.parseManifest(manifest)
    manifest = extendManifest(createManifest(2))
    suite: dict[str, object] = cast(dict[str, object], manifest["suite"])
    del suite["baseline_manifest_sha256"]
    with pytest.raises(ValidationError, match="baseline manifest"):
        support.parseManifest(manifest)


def test_allows_identical_extended_text_with_distinct_frozen_filters() -> None:
    support: type = loadSupport()
    manifest: dict[str, object] = extendManifest(createManifest(2))
    queries: list[dict[str, object]] = cast(list[dict[str, object]], manifest["queries"])
    queries[1]["text"] = queries[0]["text"]
    queries[1]["include_documents"] = ["document-one"]
    assert len(support.parseManifest(manifest).queries) == 2


def test_derived_filter_suite_retains_baseline_bytes_and_honest_provenance(
    tmp_path: Path,
) -> None:
    support: type = loadSupport()
    baselinePath: Path = tmp_path / "baseline.json"
    original: bytes = json.dumps(createManifest(), indent=2).encode()
    baselinePath.write_bytes(original)
    originalQueries: object = deepcopy(createManifest()["queries"])
    derived = support.deriveExtendedManifest(
        baselinePath, suiteId="derived-structural", suiteVersion="1"
    )
    assert baselinePath.read_bytes() == original
    assert support.parseManifest(json.loads(original)).model_dump(mode="json")["queries"]
    assert json.loads(original)["queries"] == originalQueries
    assert derived.provenance == "automated_derived"
    assert derived.reviewer is None
    assert derived.suite.kind == "extended"
    assert derived.suite.baseline_manifest_sha256 == sha256(original).hexdigest()
    assert derived.derivation["method"] == "baseline-filter-variants-v1"
    assert derived.derivation["human_relevance_reviewed"] is False
    assert len(derived.queries) == 60
    variants = {query.id: query for query in derived.queries}
    assert variants["query-0--include-marked"].expected[0][0].chunk_id == "chunk-one"
    assert variants["query-0--exclude-all"].require_empty is True
    assert variants["query-0--exclude-all"].expected == []
    assert variants["query-0--cross-namespace"].include_documents == ["document-two"]


def test_business_reviewed_cases_cannot_drop_all_evidence_groups() -> None:
    support: type = loadSupport()
    manifest: dict[str, object] = extendManifest(createManifest(1))
    queries: list[dict[str, object]] = cast(list[dict[str, object]], manifest["queries"])
    queries[0]["expected"] = []
    with pytest.raises(ValidationError, match="reviewed evidence"):
        support.parseManifest(manifest)


@pytest.mark.parametrize("field", ["permission", "revision", "source_revision", "content", "file_path", "asset_path", "pages", "section"])
def test_canonical_identity_guard_detects_each_independent_defect(field: str) -> None:
    support: type = loadSupport()
    canonical: dict[str, object] = {
        "permission": True, "revision": "revision-one", "source_revision": "revision-one",
        "content": "Canonical body", "file_path": "images/chart.jpg",
        "asset_path": "/revision-one/images/chart.jpg", "pages": [2], "section": "Manual / Chapter",
    }
    actual: dict[str, object] = {**canonical, field: "incorrect"}
    assert support.findIdentityFailures(actual, canonical) == [f"{field}: differs from canonical published data"]
    assert support.findIdentityFailures(canonical, canonical) == []
