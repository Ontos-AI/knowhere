"""Contract tests for outline_check self-verify and digest helpers."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.structure.outline_check import (
    build_tree_digest_from_entries,
    flatten_outline_entries,
    verify_entries,
)


def test_verify_entries_drops_mismatched_page_title() -> None:
    entries = [
        {"heading": "Keep Me", "level": 1, "page": 2},
        {"heading": "Missing", "level": 1, "page": 3},
        {"heading": "No Page Parent", "level": 1, "page": None},
    ]
    page_texts = {
        2: "Preface\nKeep Me\nBody",
        3: "Other chapter text only",
    }
    kept, dropped = verify_entries(entries, page_texts)
    assert [row["heading"] for row in kept] == ["Keep Me", "No Page Parent"]
    assert [row["heading"] for row in dropped] == ["Missing"]


def test_verify_entries_zero_paged_alive_means_empty_paged_kept() -> None:
    entries = [
        {"heading": "Gone", "level": 1, "page": 1},
        {"heading": "Also Gone", "level": 2, "page": 2},
    ]
    kept, dropped = verify_entries(entries, {1: "x", 2: "y"})
    assert kept == []
    assert len(dropped) == 2


def test_flatten_and_digest_preserve_levels() -> None:
    roots = [
        {
            "title": "Part A",
            "level": 1,
            "page": None,
            "children": [
                {"title": "Chapter 1", "level": 2, "page": 10, "children": []},
            ],
        }
    ]
    entries = flatten_outline_entries(roots)
    assert entries == [
        {"heading": "Part A", "level": 1, "page": None},
        {"heading": "Chapter 1", "level": 2, "page": 10},
    ]
    digest = build_tree_digest_from_entries(entries)
    assert "L1 Part A" in digest
    assert "L2 Chapter 1" in digest
