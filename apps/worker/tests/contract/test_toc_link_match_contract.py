"""Contract tests for TOC heading → link containment matching."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.structure.toc_link_enrichment import (
    TocPageLink,
    _link_dest_physical_page,
    match_toc_entries_to_links,
)


def _link(anchor: str, dest: int, *, toc_page: int = 2) -> TocPageLink:
    return TocPageLink(
        toc_page=toc_page,
        dest_physical_page=dest,
        anchor_text=anchor,
        kind=4,
    )


def test_link_dest_physical_page_by_type() -> None:
    """``int`` is 0-based; digit ``str`` is already 1-based."""
    assert _link_dest_physical_page(6) == 7
    assert _link_dest_physical_page(0) == 1
    assert _link_dest_physical_page("6") == 6
    assert _link_dest_physical_page("7") == 7


def test_match_exact_one_hit_attaches_physical_page() -> None:
    entries = [
        {"heading": " 1.投标人营业执照扫描件; ", "level": 2, "page_number": 2},
        {"heading": "二、施工组织设计", "level": 1, "page_number": 26},
    ]
    links = [
        _link("1.投标人营业执照扫描件;...............................", 7),
        _link("二、施工组织设计.............................................", 31),
        _link("无关导航链接", 99),
    ]

    enriched, matched = match_toc_entries_to_links(entries, links)

    assert matched == 2
    assert enriched[0]["link"] == {"physical_page": 7}
    assert enriched[1]["link"] == {"physical_page": 31}
    assert enriched[0]["heading"] == " 1.投标人营业执照扫描件; "
    assert enriched[0]["page_number"] == 2


def test_match_zero_or_many_hits_leaves_entry_unmatched() -> None:
    entries = [
        {"heading": "一、资格复审资料", "level": 1, "page_number": 1},
        {"heading": "共用标题", "level": 2, "page_number": 3},
    ]
    links = [
        _link("共用标题..............2", 10),
        _link("共用标题..............9", 20),
    ]

    enriched, matched = match_toc_entries_to_links(entries, links)

    assert matched == 0
    assert "link" not in enriched[0]
    assert "link" not in enriched[1]


def test_match_processes_vlm_order_once_each() -> None:
    entries = [
        {"heading": "第一章", "level": 1, "page_number": 1},
        {"heading": "第二章", "level": 1, "page_number": 5},
    ]
    links = [
        _link("第二章........5", 15),
        _link("第一章........1", 11),
    ]

    enriched, matched = match_toc_entries_to_links(entries, links)

    assert matched == 2
    assert [e["heading"] for e in enriched] == ["第一章", "第二章"]
    assert enriched[0]["link"]["physical_page"] == 11
    assert enriched[1]["link"]["physical_page"] == 15


def test_match_strips_stale_link_when_unmatched() -> None:
    entries = [
        {
            "heading": "无匹配",
            "level": 1,
            "page_number": 1,
            "link": {"physical_page": 99},
        },
    ]
    enriched, matched = match_toc_entries_to_links(entries, [_link("别的标题..1", 2)])

    assert matched == 0
    assert "link" not in enriched[0]
