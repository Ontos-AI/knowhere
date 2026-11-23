"""Public ranking remains available when hot evidence shares a query with new chunks."""

import math
import os

import pytest

os.environ.setdefault("S3_BUCKET_NAME", "contract-bucket")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")

from shared.services.retrieval.search.ranking import rank_candidates_by_path


def test_rank_keeps_root_pages_from_different_documents() -> None:
    rows: list[dict[str, object]] = [
        {
            "document_id": "target-pdf",
            "chunk_id": "target-page",
            "section_path": "Root",
            "chunk_type": "page",
            "discovery_score": 1.0,
        },
        {
            "document_id": "other-pdf",
            "chunk_id": "other-page",
            "section_path": "Root",
            "chunk_type": "page",
            "discovery_score": 0.2,
        },
    ]

    ranked = rank_candidates_by_path(rows, [], top_k=2)

    assert [row["document_id"] for row in ranked] == ["target-pdf", "other-pdf"]


@pytest.mark.parametrize(
    "importance_values",
    [
        [0.0, 0.94, 0.941, 0.942, 0.943, 0.944, 0.945, 1.0],
        [0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 1.0],
    ],
)
def test_rank_orders_equal_relevance_by_importance_within_fixed_range(
    importance_values: list[float],
) -> None:
    rows: list[dict[str, object]] = [
        {
            "document_id": "document",
            "chunk_id": f"chunk-{index}",
            "source_chunk_path": f"report.pdf/Page {index + 1}",
            "discovery_score": 1.0,
        }
        for index in range(len(importance_values))
    ]
    importance_scores: dict[str, float] = {
        f"document:chunk-{index}": importance
        for index, importance in enumerate(importance_values)
    }

    ranked: list[dict[str, object]] = rank_candidates_by_path(
        rows,
        [],
        len(rows),
        importance_scores=importance_scores,
    )

    assert len(ranked) == len(rows)
    assert {row["chunk_id"] for row in ranked} == {row["chunk_id"] for row in rows}
    for row in ranked:
        score: object = row["score"]
        multiplier: object = row["importance_multiplier"]
        assert isinstance(score, (int, float)) and math.isfinite(score)
        assert isinstance(multiplier, (int, float)) and 0.7 <= multiplier <= 1.5
    assert ranked[0]["chunk_id"] == "chunk-7"
    assert ranked[0]["importance_multiplier"] == 1.5
    assert ranked[-1]["chunk_id"] == "chunk-0"
    assert ranked[-1]["importance_multiplier"] == 0.7


def test_rank_multiplier_depends_only_on_the_candidate_itself() -> None:
    def multiplier_of_first(other_importance: float) -> object:
        rows: list[dict[str, object]] = [
            {"document_id": "a", "chunk_id": "a1", "discovery_score": 1.0},
            {"document_id": "b", "chunk_id": "b1", "discovery_score": 1.0},
        ]
        ranked = rank_candidates_by_path(
            rows, [], 2, importance_scores={"a:a1": 0.5, "b:b1": other_importance}
        )
        return next(row for row in ranked if row["chunk_id"] == "a1")["importance_multiplier"]

    assert multiplier_of_first(0.0) == multiplier_of_first(1.0) == 1.1


def test_rank_uses_document_importance_for_unused_chunks() -> None:
    rows: list[dict[str, object]] = [
        {"document_id": "fresh", "chunk_id": "never-used", "discovery_score": 1.0},
        {"document_id": "stale", "chunk_id": "used", "discovery_score": 1.0},
    ]

    ranked = rank_candidates_by_path(
        rows,
        [],
        2,
        importance_scores={"fresh:": 0.3, "stale:": 0.0, "stale:used": 0.1},
    )

    by_chunk = {row["chunk_id"]: row for row in ranked}
    assert by_chunk["never-used"]["importance"] == 0.3
    assert by_chunk["used"]["importance"] == 0.1
    assert [row["chunk_id"] for row in ranked] == ["never-used", "used"]


def test_rank_without_known_importance_keeps_relevance() -> None:
    rows: list[dict[str, object]] = [
        {"document_id": "a", "chunk_id": "a1", "discovery_score": 0.8},
    ]

    ranked = rank_candidates_by_path(rows, [], 1, importance_scores={})

    assert ranked[0]["importance_multiplier"] == 1.0
    assert "importance" not in ranked[0]
