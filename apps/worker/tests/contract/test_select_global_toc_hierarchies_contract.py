"""Contracts for primary vs pending TOC split (no proximity merge)."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.structure.toc_anchoring import (
    select_global_toc_hierarchies,
)


def test_nearby_second_toc_is_pending_not_merged_into_primary() -> None:
    """Former front-cluster gap (≤5 pages) must not pull a later TOC into primary."""
    hierarchies = [
        {
            "toc_range": [1, 1],
            "toc_range_unit": "page",
            "toc_with_level": [{"heading": "Front", "level": 1, "page_number": 2}],
        },
        {
            "toc_range": [4, 4],
            "toc_range_unit": "page",
            "toc_with_level": [{"heading": "Near", "level": 1, "page_number": 5}],
        },
        {
            "toc_range": [40, 40],
            "toc_range_unit": "page",
            "toc_with_level": [{"heading": "Ch3", "level": 1, "page_number": 1}],
        },
    ]
    primary, pending, summary = select_global_toc_hierarchies(
        hierarchies=hierarchies,
        filename="doc.pdf",
    )
    assert primary == [hierarchies[0]]
    assert pending == [hierarchies[1], hierarchies[2]]
    assert summary["strategy"] == "earliest_forest_rest_pending"
    assert summary["primary_count"] == 1
    assert summary["pending_count"] == 2
