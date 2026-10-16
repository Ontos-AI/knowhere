"""TOC candidate selection after Stage-0 line records exist."""

from __future__ import annotations

from app.services.document_agent.pdf_text import (
    LineDraft,
    PageTextLine,
    build_page_text_bands,
    line_fingerprint,
)
from app.services.document_agent.tools.find_toc_anchor_pages import (
    HEAD_WINDOW_MAX_PAGES,
    MAX_ANCHOR_CANDIDATES,
    _filter_edge_anchored,
    _head_window_pages,
    _prefix_toc_keywords_for_categories,
    _scan_toc_matches,
    _select_anchor_pages,
)
from app.services.document_parser.profiling.domain_taxonomy import (
    ECONOMY_BUSINESS_FINANCE_CODE,
)


def _line(
    text: str,
    *,
    region: str = "body",
    height: float | None = 10.0,
    font: str | None = "Helv",
) -> PageTextLine:
    return PageTextLine(
        text=text,
        region=region,  # type: ignore[arg-type]
        line_index=0,
        height=height,
        font=font,
        font_size=10.0,
    )


def _match(
    page: int,
    text: str,
    *,
    region: str = "body",
    kind: str = "keyword:contents",
    height: float | None = 10.0,
    font: str | None = "Helv",
) -> dict:
    line = _line(text, region=region, height=height, font=font)
    return {
        "page": page,
        "raw_line": text,
        "line_index": 0,
        "line_end_index": 0,
        "match_kind": kind,
        "region": region,
        "fingerprint": line_fingerprint(line),
    }


def test_consecutive_same_signature_drops_run_and_keeps_unique_neighbor() -> None:
    unique = _match(17, "Table of Contents\nTABLE OF CONTENTS")
    run = [_match(page, "Table of Contents") for page in range(18, 61)]
    pages, narrowing = _select_anchor_pages([unique, *run])
    assert pages == {17}
    assert narrowing["after_dedup"] == 1
    assert "after_edge" not in narrowing


def test_same_signature_on_nonconsecutive_pages_is_kept() -> None:
    matches = [_match(page, "Contents") for page in (5, 284, 361)]
    pages, narrowing = _select_anchor_pages(matches)
    assert pages == {5, 284, 361}
    assert narrowing["after_dedup"] == 3


def test_two_consecutive_same_signature_pages_are_dropped() -> None:
    pages, _narrowing = _select_anchor_pages(
        [_match(10, "Contents"), _match(11, "Contents")]
    )
    assert pages == set()


def test_edge_anchor_keeps_suffix_and_cjk_drops_mid_line_english() -> None:
    kept = _filter_edge_anchored(
        [
            _match(1, "General table of contents", kind="keyword:table of contents"),
            _match(
                2,
                "Commentary provides guidance on minimum cement contents in different situations.",
            ),
            _match(
                3, "The basic contents of a typical contract document are shown below:"
            ),
            _match(4, "See 目录 in this sentence.", kind="keyword:目录"),
        ]
    )
    assert [item["page"] for item in kept] == [1, 4]


def test_over_cap_all_body_gives_up() -> None:
    matches = [
        _match(page, f"Contents {page}", height=float(page))
        for page in range(1, MAX_ANCHOR_CANDIDATES + 2)
    ]
    pages, narrowing = _select_anchor_pages(matches)
    assert pages == set()
    assert narrowing["gave_up"] is True
    assert narrowing["after_body"] == MAX_ANCHOR_CANDIDATES + 1


def test_over_cap_body_filter_keeps_body_hits() -> None:
    body = [
        _match(page, f"Contents {page}", height=float(page))
        for page in range(1, MAX_ANCHOR_CANDIDATES)
    ]
    headers = [
        _match(
            MAX_ANCHOR_CANDIDATES,
            "Contents header",
            region="header",
            height=1.0,
        ),
        _match(
            MAX_ANCHOR_CANDIDATES + 1,
            "Contents header two",
            region="header",
            height=2.0,
        ),
    ]
    pages, narrowing = _select_anchor_pages([*body, *headers])
    assert pages == {page for page in range(1, MAX_ANCHOR_CANDIDATES)}
    assert narrowing["after_body"] == MAX_ANCHOR_CANDIDATES - 1
    assert "gave_up" not in narrowing


def test_under_cap_does_not_apply_edge_or_body() -> None:
    pages, narrowing = _select_anchor_pages(
        [_match(1, "Contents"), _match(3, "See contents here")]
    )
    assert pages == {1, 3}
    assert "after_edge" not in narrowing
    assert "after_body" not in narrowing


def test_head_window_is_at_most_ten_pages() -> None:
    assert _head_window_pages(0) == set()
    assert _head_window_pages(5) == {1, 2, 3, 4, 5}
    assert _head_window_pages(10) == set(range(1, 11))
    assert _head_window_pages(200) == set(range(1, HEAD_WINDOW_MAX_PAGES + 1))


def test_scan_uses_stage0_lines() -> None:
    bands = {
        1: build_page_text_bands((LineDraft(text="General table of contents"),)),
        2: build_page_text_bands((LineDraft(text="No keyword here"),)),
    }
    matches = _scan_toc_matches(bands, page_count=2)
    assert [item["page"] for item in matches] == [1]
    assert matches[0]["region"] == "body"


def test_economy_category_adds_finance_prefix_toc_keywords() -> None:
    assert _prefix_toc_keywords_for_categories(
        [ECONOMY_BUSINESS_FINANCE_CODE]
    ) == frozenset({"index to", "index of", "exhibit index"})


def test_exhibit_index_prefix_keyword_applies_to_every_domain() -> None:
    assert _prefix_toc_keywords_for_categories(["13000000"]) == frozenset(
        {"exhibit index"}
    )
    assert _prefix_toc_keywords_for_categories(None) == frozenset({"exhibit index"})


def test_finance_prefix_keywords_match_only_at_line_start_when_enabled() -> None:
    bands = {
        1: build_page_text_bands(
            (LineDraft(text="INDEX TO FINANCIAL STATEMENTS"),)
        ),
        2: build_page_text_bands(
            (LineDraft(text="INDEX OF FINANCIAL STATEMENTS"),)
        ),
        3: build_page_text_bands((LineDraft(text="EXHIBIT INDEX"),)),
        4: build_page_text_bands((LineDraft(text="INDEX TO EXHIBITS"),)),
        5: build_page_text_bands(
            (LineDraft(text="See the index to financial statements below."),)
        ),
        6: build_page_text_bands(
            (LineDraft(text="See the exhibit index below."),)
        ),
        7: build_page_text_bands((LineDraft(text="No keyword here"),)),
    }
    without = _scan_toc_matches(bands, page_count=7)
    assert without == []
    with_prefix = _scan_toc_matches(
        bands,
        page_count=7,
        prefix_keywords=frozenset({"index to", "index of", "exhibit index"}),
    )
    assert [(item["page"], item["match_kind"]) for item in with_prefix] == [
        (1, "keyword:index to"),
        (2, "keyword:index of"),
        (3, "keyword:exhibit index"),
        (4, "keyword:index to"),
    ]
