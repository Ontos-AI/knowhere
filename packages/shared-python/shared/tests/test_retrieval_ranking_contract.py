"""Public ranking remains available when hot evidence shares a query with new chunks."""

import math
import os

import pytest

os.environ.setdefault("S3_BUCKET_NAME", "contract-bucket")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")

from shared.services.retrieval.search.ranking import rank_candidates_by_path


@pytest.mark.parametrize(
    "importance_values",
    [
        [0.0, 1000.0, 1000.001, 1000.002, 1000.003, 1000.004, 1000.005, 1000.006],
        [0.0, 0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 1000.0],
    ],
)
def test_rank_hot_and_new_evidence_without_score_overflow(
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

    ranked = rank_candidates_by_path(
        rows,
        [],
        len(rows),
        importance_scores=importance_scores,
    )

    assert len(ranked) == len(rows)
    assert {row["chunk_id"] for row in ranked} == {row["chunk_id"] for row in rows}
    assert all(math.isfinite(float(row["score"])) for row in ranked)
    assert all(0.1 <= float(row["importance_multiplier"]) <= 2.0 for row in ranked)
    assert ranked[0]["importance_raw_score"] >= ranked[-1]["importance_raw_score"]
