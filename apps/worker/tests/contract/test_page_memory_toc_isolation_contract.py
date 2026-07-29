"""Contract tests for TOC page isolation policy and node restore."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.document_agent.manifest import TocRegionBoundary, TocResult
from app.services.page_memory._utils import slice_text_from_anchor
from app.services.page_memory.fine_hierarchy import compute_fat_leaf_pages
from app.services.page_memory.node_assembler import (
    build_toc_node_rows,
    format_toc_entries_content,
    merge_rows_by_first_page,
)
from app.services.page_memory.skeleton_extractor import SectionSkeleton
from app.services.page_memory.toc_page_policy import TocPagePolicy


def test_toc_page_policy_keeps_mixed_page_in_processing() -> None:
    anatomy = SimpleNamespace(
        toc_result=TocResult(
            toc_pages=[3, 4, 5, 6],
            regions=[
                TocRegionBoundary(
                    toc_pages=[3, 4, 5, 6],
                    pure_toc_pages=[3, 4, 5],
                    mixed_page=6,
                    body_start_text="1 Scope",
                    reason="mixed",
                )
            ],
            method="vlm_batch",
        )
    )
    policy = TocPagePolicy.from_anatomy(anatomy)
    assert policy.pure_toc_pages == frozenset({3, 4, 5})
    assert policy.body_start_text(6) == "1 Scope"
    assert policy.filter_processing_pages([1, 2, 3, 4, 5, 6, 7]) == [1, 2, 6, 7]


def test_toc_page_policy_fallback_excludes_all_toc_pages() -> None:
    anatomy = SimpleNamespace(
        toc_result=TocResult(toc_pages=[2, 3], method="vlm_batch")
    )
    policy = TocPagePolicy.from_anatomy(anatomy)
    assert policy.pure_toc_pages == frozenset({2, 3})
    assert policy.filter_processing_pages([1, 2, 3, 4]) == [1, 4]


def test_fat_leaf_pages_exclude_pure_toc() -> None:
    skeletons = [
        SectionSkeleton(
            section_path="doc/A",
            level=1,
            start_page=3,
            end_page=10,
            title="A",
            parent_path="doc",
        )
    ]
    pages = compute_fat_leaf_pages(
        skeletons,
        min_pages=4,
        exclude_pages={3, 4, 5},
    )
    assert 3 not in pages
    assert 6 in pages


def test_slice_text_from_anchor_keeps_tail() -> None:
    text = "Contents .... 1\n1.2 Title\nBody paragraph"
    sliced, matched = slice_text_from_anchor(text, "1.2 Title")
    assert matched is True
    assert sliced.startswith("1.2 Title")


def test_build_toc_node_rows_bypass_same_as() -> None:
    anatomy = SimpleNamespace(
        toc_hierarchies=[
            {
                "toc_range": [3, 6],
                "toc_with_level": [
                    {"heading": "1 Scope", "level": 1, "page_number": 1},
                    {"heading": "1.1 Terms", "level": 2, "page_number": 2},
                ],
            }
        ],
        toc_result=TocResult(
            toc_pages=[3, 4, 5, 6],
            regions=[
                TocRegionBoundary(
                    toc_pages=[3, 4, 5, 6],
                    pure_toc_pages=[3, 4, 5],
                    mixed_page=6,
                    body_start_text="1 Scope",
                    reason="mixed",
                )
            ],
            method="vlm_batch",
        ),
    )
    rows = build_toc_node_rows(anatomy=anatomy, filename="doc.pdf")
    assert len(rows) == 1
    row = rows[0]
    assert row["type"] == "page"
    assert row["extra_metadata"]["content_kind"] == "table_of_contents"
    assert row["page_nums"] == "3,4,5,6"
    assert "1 Scope" in row["content"]
    assert "SAME-AS" not in row["content"]


def test_merge_rows_inserts_toc_before_body_on_shared_page() -> None:
    toc_rows = [
        {
            "path": "doc/Table of Contents",
            "type": "page",
            "page_nums": "6",
            "extra_metadata": {"content_kind": "table_of_contents"},
        }
    ]
    body_rows = [
        {
            "path": "doc/1 Scope",
            "type": "page",
            "page_nums": "6,7",
            "extra_metadata": {},
        }
    ]
    merged = merge_rows_by_first_page(toc_rows, body_rows)
    assert [row["path"] for row in merged] == [
        "doc/Table of Contents",
        "doc/1 Scope",
    ]


def test_format_toc_entries_content_indents_by_level() -> None:
    content = format_toc_entries_content(
        [
            {"heading": "1 Scope", "level": 1, "page_number": 1},
            {"heading": "1.1 Terms", "level": 2, "page_number": 2},
        ]
    )
    assert content.splitlines()[0].startswith("1 Scope")
    assert content.splitlines()[1].startswith("  1.1 Terms")
