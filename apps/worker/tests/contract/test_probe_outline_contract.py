"""Contract tests for probe.outline forest prune and physical pages."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.tools.probe_outline import build_outline_forest


def test_outline_keeps_no_page_parent_with_paged_children() -> None:
    forest = build_outline_forest(
        [
            [1, "Part A", -1],
            [2, "Chapter 1", 10],
            [2, "Chapter 2", 20],
        ]
    )
    assert len(forest) == 1
    assert forest[0]["title"] == "Part A"
    assert forest[0]["page"] is None
    assert [child["title"] for child in forest[0]["children"]] == [
        "Chapter 1",
        "Chapter 2",
    ]
    assert [child["page"] for child in forest[0]["children"]] == [10, 20]


def test_outline_drops_no_page_leaf_and_empty_subtree() -> None:
    forest = build_outline_forest(
        [
            [1, "Keep", 5],
            [1, "DropLeaf", -1],
            [1, "DropParent", -1],
            [2, "DropChild", 0],
        ]
    )
    assert [node["title"] for node in forest] == ["Keep"]
