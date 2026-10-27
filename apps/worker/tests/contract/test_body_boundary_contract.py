"""Contracts for TOC-derived body-boundary helpers."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_parser.structure.body_boundary import (
    clean_toc_title,
    normalize_match_text,
)
from app.services.document_parser.structure.layout_parser import (
    _supports_multi_toc_zones,
)


def test_normalize_match_text_uses_cjk_aware_spacing_and_lowercase() -> None:
    assert normalize_match_text("附录 A OVERVIEW") == "附录a overview"
    assert normalize_match_text("Public\n  Domain\tManual") == "public domain manual"
    assert normalize_match_text("目\n录") == "目录"
    assert normalize_match_text("Chapter 1 概述") == "chapter 1概述"


def test_clean_toc_title_strips_numbering_and_page_suffix() -> None:
    assert clean_toc_title("1.2 Overview ... 14") == "Overview"
    assert clean_toc_title("第一章 概述 3") == "概述"


def test_multi_toc_zones_enabled_for_md_and_docx_smart_parse() -> None:
    line_tocs = [
        {"toc_range": [1, 10]},
        {"toc_range": [30, 40]},
    ]

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
    assert not _supports_multi_toc_zones(
        line_tocs,
        doc_type="md",
        smart_parse=False,
    )
