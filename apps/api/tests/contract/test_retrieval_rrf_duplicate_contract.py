"""Contracts for duplicate chunk handling in reciprocal-rank fusion."""

from __future__ import annotations

from shared.services.retrieval.search.scoring import merge_channels_rrf


def test_rrf_counts_each_chunk_once_per_channel() -> None:
    rows = [
        {"chunk_id": "shared", "document_id": "doc-a"},
        {"chunk_id": "shared", "document_id": "doc-b"},
        {"chunk_id": "other", "document_id": "doc-c"},
    ]

    result = merge_channels_rrf([rows], [1.0], top_k=3)

    assert [row["chunk_id"] for row in result] == ["shared", "other"]
    assert result[0]["score"] == round(1.0 / 61, 6)
    assert result[1]["score"] == round(1.0 / 62, 6)
