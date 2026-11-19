"""Unnumbered TOCs are kept for their pages: no anchoring, one TOC node each."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.structure.anchoring_primitives import (
    SkeletonAnchor,
    serialize_skeleton_anchor,
    serialize_title_node,
)
from app.services.document_agent.structure.hierarchy_locator import (
    TitleMatch,
    TitleNode,
)
from app.services.document_agent.structure.toc_anchoring import (
    is_unnumbered_toc,
    select_global_toc_hierarchies,
    split_unnumbered_tocs,
)
from app.services.page_memory._utils import is_toc_node
from app.services.page_memory.skeleton_extractor import (
    SectionSkeleton,
    extract_section_skeletons,
)


def _numbered_toc(
    toc_range: list[int],
    rows: list[tuple[str, int]],
) -> dict[str, Any]:
    return {
        "toc_range": toc_range,
        "toc_range_unit": "page",
        "toc_with_level": [
            {"heading": title, "level": 1, "page_number": page} for title, page in rows
        ],
    }


def _unnumbered_toc(toc_range: list[int]) -> dict[str, Any]:
    return {
        "toc_range": toc_range,
        "toc_range_unit": "page",
        "toc_with_level": [
            {"heading": "1 Intro", "level": 1, "page_number": None},
            {"heading": "2 Scope", "level": 1, "page_number": None},
        ],
    }


def _extract(
    *,
    page_count: int,
    chapters: list[tuple[str, int]],
    hierarchies: list[dict[str, Any]],
    toc_pages: list[int],
) -> list[SectionSkeleton]:
    overrides = {
        (title,): TitleMatch(
            page=page,
            source="anchored",
            matched_line=title,
            candidates=[page],
            evidence={},
        )
        for title, page in chapters
    }
    anchor = SkeletonAnchor(
        offset=0,
        offset_status="ok",
        match_overrides=overrides,
        null_page_report=[],
        bulk_count=len(overrides),
        pruned_count=0,
        locate_method="offset_guided_bulk",
    )
    anatomy = SimpleNamespace(
        page_count=page_count,
        toc_result=SimpleNamespace(toc_pages=toc_pages),
        toc_hierarchies=hierarchies,
        skeleton_anchor=serialize_skeleton_anchor(anchor),
        skeleton_nodes=[
            serialize_title_node(TitleNode(title=title, level=1, printed_page=page))
            for title, page in chapters
        ],
        pending_skeleton_anchors=[],
    )
    return extract_section_skeletons(
        anatomy=anatomy,
        filename="doc.pdf",
        page_texts={page: "body" for page in range(1, page_count + 1)},
    )


def _rows(skeletons: list[SectionSkeleton]) -> list[tuple[str, int, int]]:
    return [(item.section_path, item.start_page, item.end_page) for item in skeletons]


def _toc_nodes(skeletons: list[SectionSkeleton]) -> list[tuple[str, str, int, int]]:
    return sorted(
        (item.section_path, item.parent_path or "", item.start_page, item.end_page)
        for item in skeletons
        if is_toc_node(item)
    )


MAIN_TOC = _numbered_toc([2, 2], [("Ch1", 3), ("Ch2", 20)])
NUMBERED_ONLY_ROWS = [
    ("doc.pdf", 1, 1),
    ("doc.pdf/Ch1", 3, 20),
    ("doc.pdf/Ch2", 20, 40),
    ("doc.pdf/TOC", 2, 2),
]


# -- Selection -----------------------------------------------------------------


def test_unnumbered_definition() -> None:
    assert is_unnumbered_toc(_unnumbered_toc([5, 5]))
    assert not is_unnumbered_toc(MAIN_TOC)
    partly_numbered = _unnumbered_toc([5, 5])
    partly_numbered["toc_with_level"][1]["page_number"] = 7
    assert not is_unnumbered_toc(partly_numbered)
    blank_labels = _unnumbered_toc([5, 5])
    blank_labels["toc_with_level"][0]["page_number"] = " "
    assert is_unnumbered_toc(blank_labels)
    outline = {"source": "pdf_outline", "toc_with_level": [{"heading": "A"}]}
    assert not is_unnumbered_toc(outline)


def test_unnumbered_tocs_never_reach_primary_or_pending() -> None:
    early_unnumbered = _unnumbered_toc([1, 1])
    late_unnumbered = _unnumbered_toc([30, 30])
    nested = _numbered_toc([40, 40], [("A", 41)])
    numbered, unnumbered = split_unnumbered_tocs(
        [early_unnumbered, MAIN_TOC, late_unnumbered, nested]
    )

    assert unnumbered == [early_unnumbered, late_unnumbered]
    primary, pending, _summary = select_global_toc_hierarchies(
        hierarchies=numbered,
        filename="doc.pdf",
    )
    assert primary == [MAIN_TOC]
    assert pending == [nested]


# -- TOC nodes -------------------------------------------------------------------


def test_numbered_tocs_only_give_todays_rows() -> None:
    skeletons = _extract(
        page_count=40,
        chapters=[("Ch1", 3), ("Ch2", 20)],
        hierarchies=[MAIN_TOC],
        toc_pages=[2],
    )

    assert _rows(skeletons) == NUMBERED_ONLY_ROWS


def test_unnumbered_toc_gets_a_node_and_no_row_changes() -> None:
    skeletons = _extract(
        page_count=40,
        chapters=[("Ch1", 3), ("Ch2", 20)],
        hierarchies=[MAIN_TOC, _unnumbered_toc([10, 11])],
        toc_pages=[2, 10, 11],
    )

    assert _rows(skeletons) == [*NUMBERED_ONLY_ROWS, ("doc.pdf/Ch1/TOC", 10, 11)]
    node = next(item for item in skeletons if item.start_page == 10)
    assert is_toc_node(node)
    assert node.parent_path == "doc.pdf/Ch1"
    assert node.level == 2


def test_toc_on_boundary_page_goes_under_section_opening_there() -> None:
    skeletons = _extract(
        page_count=40,
        chapters=[("Ch1", 3), ("Ch2", 20)],
        hierarchies=[MAIN_TOC, _unnumbered_toc([20, 20])],
        toc_pages=[2, 20],
    )

    # 20 is Ch2's start and Ch1's shared last page; Ch2 is the open section.
    assert ("doc.pdf/Ch2/TOC", "doc.pdf/Ch2", 20, 20) in _toc_nodes(skeletons)


def test_distinct_tocs_under_one_parent_are_named_by_first_page() -> None:
    skeletons = _extract(
        page_count=40,
        chapters=[("Ch1", 3), ("Ch2", 20)],
        hierarchies=[MAIN_TOC, _unnumbered_toc([24, 24]), _unnumbered_toc([30, 31])],
        toc_pages=[2, 24, 30, 31],
    )

    assert _toc_nodes(skeletons) == [
        ("doc.pdf/Ch2/TOC p.24", "doc.pdf/Ch2", 24, 24),
        ("doc.pdf/Ch2/TOC p.30", "doc.pdf/Ch2", 30, 31),
        ("doc.pdf/TOC", "doc.pdf", 2, 2),
    ]
    assert {item.title for item in skeletons if is_toc_node(item)} == {
        "TOC",
        "TOC p.24",
        "TOC p.30",
    }


def test_root_fallback_without_numbered_toc_is_unchanged() -> None:
    skeletons = _extract(
        page_count=40,
        chapters=[],
        hierarchies=[_unnumbered_toc([12, 12]), _unnumbered_toc([25, 26])],
        toc_pages=[12, 25, 26],
    )

    assert _rows(skeletons) == [("doc.pdf/Root", 1, 40)]
    assert skeletons[0].evidence == {"source": "fallback_root", "reason": "no_toc"}
