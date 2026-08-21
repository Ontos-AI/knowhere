"""Contracts for TOC-derived body-boundary helpers used by TEXT-TRACK."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_parser.structure.body_boundary import (
    extract_level1_titles,
    find_first_body_boundary,
    normalize_match_text,
)
from app.services.document_parser.structure.layout_parser import (
    _supports_multi_toc_zones,
)


def test_extract_level1_titles_reads_toc_with_level_not_toc_tree() -> None:
    titles = extract_level1_titles(
        [
            {
                "toc_range": [1, 20],
                "toc_range_unit": "page",
                "source": "calibrated_shard_split",
                # Legacy-shaped tree must be ignored: production slices omit it.
                "toc_tree": {"Ignore Me": {}},
                "toc_with_level": [
                    {"heading": "1. Overview", "level": 1},
                    {"heading": "1.1 Scope", "level": 2},
                    {"heading": "2. Requirements", "level": 1},
                ],
            }
        ]
    )
    assert titles == ["Overview", "Requirements"]


def test_normalize_match_text_uses_cjk_aware_spacing_and_lowercase() -> None:
    assert normalize_match_text("附录 A OVERVIEW") == "附录a overview"
    assert normalize_match_text("Public\n  Domain\tManual") == "public domain manual"
    assert normalize_match_text("目\n录") == "目录"
    assert normalize_match_text("Chapter 1 概述") == "chapter 1概述"


def test_extract_level1_titles_ignores_empty_or_non_list_payloads() -> None:
    assert extract_level1_titles([]) == []
    assert extract_level1_titles([{"toc_with_level": None}]) == []
    assert extract_level1_titles([{"toc_with_level": "| heading | level |"}]) == []
    assert extract_level1_titles([{"toc_tree": {"Only Tree": {}}}]) == []


def test_find_first_body_boundary_matches_cleaned_level1_in_md_lines() -> None:
    boundary = find_first_body_boundary(
        [
            "Cover Page",
            "Legal Notice",
            "# 1. Overview",
            "Body text",
        ],
        ["Overview"],
    )
    assert boundary == 2


def test_multi_toc_zones_are_isolated_from_profile_page_coordinates() -> None:
    page_tocs = [
        {"toc_range": [1, 10], "toc_range_unit": "page"},
        {"toc_range": [11, 20], "toc_range_unit": "page"},
    ]
    line_tocs = [
        {"toc_range": [1, 10]},
        {"toc_range": [30, 40]},
    ]

    assert not _supports_multi_toc_zones(
        page_tocs,
        doc_type="md",
        smart_parse=True,
    )
    assert _supports_multi_toc_zones(
        line_tocs,
        doc_type="md",
        smart_parse=True,
    )
    assert _supports_multi_toc_zones(
        line_tocs,
        doc_type="docx",
        smart_parse=True,
    )
