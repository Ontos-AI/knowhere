"""Unit tests for null-page parent locate (compact-strict + self-only emit)."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.structure.hierarchy_locator import (
    TitleMatch,
    TitleNode,
    locate_title_compact_strict,
    resolve_hierarchy_page_ranges,
)
from app.services.page_memory.skeleton_extractor import (
    locate_null_page_parent_overrides,
)


def _leaf_match(page: int) -> TitleMatch:
    return TitleMatch(
        page=page,
        confidence=0.9,
        source="agent_vlm",
        matched_line="",
        score=0.9,
        candidates=[page],
        evidence={},
    )


def _example_tree() -> list[TitleNode]:
    return [
        TitleNode(
            title="Chapter 1",
            level=1,
            printed_page=10,
            children=[
                TitleNode(title="1.1 Intro", level=2, printed_page=10),
                TitleNode(title="1.2 Details", level=2, printed_page=12),
            ],
        ),
        TitleNode(
            title="Chapter 2 Overview",
            level=1,
            printed_page=None,
            children=[
                TitleNode(title="2.1 Setup", level=2, printed_page=15),
                TitleNode(title="2.2 Results", level=2, printed_page=18),
            ],
        ),
    ]


def _example_leaf_overrides() -> dict[tuple[str, ...], TitleMatch]:
    return {
        ("Chapter 1", "1.1 Intro"): _leaf_match(12),
        ("Chapter 1", "1.2 Details"): _leaf_match(14),
        ("Chapter 2 Overview", "2.1 Setup"): _leaf_match(17),
        ("Chapter 2 Overview", "2.2 Results"): _leaf_match(20),
    }


def test_compact_strict_matches_cross_line_title() -> None:
    match = locate_title_compact_strict(
        "Chapter 2 Overview",
        scope_pages=[14, 15, 16, 17],
        page_texts={
            14: "1.2 Details body",
            15: "Chapter 2\nOverview\nIntro paragraph",
            16: "more intro",
            17: "2.1 Setup",
        },
    )
    assert match is not None
    assert match.page == 15
    assert match.evidence.get("accept") == "compact_strict_unique"


def test_compact_strict_rejects_ambiguous_pages() -> None:
    match = locate_title_compact_strict(
        "Chapter 2 Overview",
        scope_pages=[14, 15, 16, 17],
        page_texts={
            14: "x",
            15: "Chapter 2\nOverview",
            16: "Chapter 2 Overview again",
            17: "2.1 Setup",
        },
    )
    assert match is None


def test_resolve_emits_parent_self_only_and_truncates_prev_leaf() -> None:
    nodes = _example_tree()
    overrides = _example_leaf_overrides()
    overrides[("Chapter 2 Overview",)] = TitleMatch(
        page=15,
        confidence=0.92,
        source="anchored",
        matched_line="chapter2overview",
        score=0.96,
        candidates=[15],
        evidence={"accept": "compact_strict_unique"},
    )
    page_texts = {page: "" for page in range(12, 21)}
    ranges = resolve_hierarchy_page_ranges(
        nodes,
        page_count=20,
        page_texts=page_texts,
        body_pages=list(range(12, 21)),
        match_overrides=overrides,
    )
    by_path = {item.path_titles: item for item in ranges}

    details = by_path[("Chapter 1", "1.2 Details")]
    assert details.end_page == 15

    parent = by_path[("Chapter 2 Overview",)]
    assert parent.start_page == 15
    assert parent.end_page == 17
    assert parent.evidence.get("skeleton_kind") == "parent_self_only"

    setup = by_path[("Chapter 2 Overview", "2.1 Setup")]
    assert setup.start_page == 17


def test_locate_null_page_parent_overrides_uses_compact_window() -> None:
    nodes = _example_tree()
    overrides, report = locate_null_page_parent_overrides(
        nodes=nodes,
        match_overrides=_example_leaf_overrides(),
        page_texts={
            14: "1.2 body",
            15: "Chapter 2\nOverview",
            16: "intro",
            17: "2.1 Setup",
            20: "2.2 Results",
        },
        body_pages=list(range(12, 21)),
        ctx=None,
    )
    parent = overrides[("Chapter 2 Overview",)]
    assert parent.page == 15
    assert parent.evidence.get("accept") == "compact_strict_unique"
    assert any(row["title"] == "Chapter 2 Overview" and row["page"] == 15 for row in report)


def test_locate_null_page_parent_visual_rtl_when_text_ambiguous(
    monkeypatch: Any,
) -> None:
    nodes = _example_tree()
    calls: list[int] = []

    def _fake_verify(
        *,
        ctx: Any,
        title: str,
        candidate_matches: list[TitleMatch],
        candidate_page_cap: int,
    ) -> dict[str, Any]:
        page = candidate_matches[0].page
        if title == "Chapter 2 Overview":
            calls.append(page)
        if title == "Chapter 2 Overview" and page == 15:
            return {
                "selected_page": 15,
                "confidence": 0.8,
                "source": "agent_vlm",
                "reason": "title heading",
            }
        return {
            "selected_page": None,
            "confidence": 0.2,
            "source": "agent_vlm",
            "reason": "not here",
        }

    monkeypatch.setattr(
        "app.services.page_memory.skeleton_extractor.verify_section_page_choice",
        _fake_verify,
    )
    overrides, report = locate_null_page_parent_overrides(
        nodes=nodes,
        match_overrides=_example_leaf_overrides(),
        page_texts={
            14: "x",
            15: "Chapter 2\nOverview",
            16: "Chapter 2 Overview",
            17: "2.1 Setup",
            20: "2.2",
        },
        body_pages=list(range(12, 21)),
        ctx=MagicMock(),
    )
    parent = overrides[("Chapter 2 Overview",)]
    assert parent.page == 15
    assert parent.evidence.get("accept") == "visual_rtl"
    assert calls[0] == 17
    assert 15 in calls
    assert calls.index(15) > calls.index(17)
    entry = next(row for row in report if row["title"] == "Chapter 2 Overview")
    assert entry["visual_verify_calls"] >= 1
    assert entry["result"] == "visual_rtl"
