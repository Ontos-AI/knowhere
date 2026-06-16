"""Line-based body boundary helpers for TOC-derived headings."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


_LEADING_NUMBER_RE = re.compile(
    r"""^
    (?:
        [#]+\s*
        | 第\s*[零一二三四五六七八九十百千\d]+\s*[章节篇部分]
        | [零一二三四五六七八九十百千]+\s*[、。，,]
        | [（(]\s*[零一二三四五六七八九十百千\d]+\s*[）)]
        | \d+(?:\.\d+)*\.?\s*
        | [IVXLCDM]+\.?\s+
        | [A-Za-z]\.\s+
        | Chapter\s+\w+\s*
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

_PAGE_SUFFIX_RE = re.compile(r"[\s\.\-·…]+\d+\s*$")


def normalize_heading_text(text: str) -> str:
    """Normalize text for fuzzy heading matching."""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_toc_title(title: str) -> str:
    """Remove leading numbering/hashes and trailing page numbers from a TOC title."""
    cleaned = _PAGE_SUFFIX_RE.sub("", title or "").strip()
    cleaned = _LEADING_NUMBER_RE.sub("", cleaned).strip()
    return cleaned


def extract_level1_titles(toc_hierarchies: list[dict[str, Any]]) -> list[str]:
    """Extract cleaned level-1 titles from TOC hierarchy payloads."""
    titles: list[str] = []
    for hier in toc_hierarchies:
        toc_tree = hier.get("toc_tree") or {}
        for raw_title in toc_tree.keys():
            cleaned = clean_toc_title(str(raw_title))
            if cleaned and len(cleaned) >= 2:
                titles.append(cleaned)
    return titles


def find_first_body_boundary(
    lines: list[str],
    level1_titles: list[str],
) -> int | None:
    """Return the first line index matching a TOC level-1 title, if any."""
    normalized_titles = [
        normalize_heading_text(title)
        for title in level1_titles
        if normalize_heading_text(title)
    ]
    if not normalized_titles:
        return None

    for index, line in enumerate(lines):
        normalized_line = normalize_heading_text(line.lstrip("#").strip())
        if any(title in normalized_line for title in normalized_titles):
            return index
    return None
