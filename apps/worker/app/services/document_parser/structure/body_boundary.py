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


def normalize_heading_label(text: str) -> str:
    """Normalize heading text while preserving display casing."""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_cjk_char(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3000 <= codepoint <= 0x303F
        or 0x3040 <= codepoint <= 0x30FF
        or 0x31F0 <= codepoint <= 0x31FF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x1100 <= codepoint <= 0x11FF
        or 0x3130 <= codepoint <= 0x318F
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0x20000 <= codepoint <= 0x2FA1F
    )


def normalize_match_text(text: str) -> str:
    """Normalize query and corpus text for every deterministic text match.

    Whitespace becomes one space between non-CJK text, and disappears whenever
    either adjacent character is CJK. Matching is case-insensitive.
    """
    normalized = unicodedata.normalize("NFKC", text or "").casefold().strip()
    parts = re.split(r"\s+", normalized)
    if not parts or not parts[0]:
        return ""

    output = parts[0]
    for part in parts[1:]:
        if not part:
            continue
        separator = "" if _is_cjk_char(output[-1]) or _is_cjk_char(part[0]) else " "
        output = f"{output}{separator}{part}"
    return output


def clean_toc_title(title: str) -> str:
    """Remove leading numbering/hashes and trailing page numbers from a TOC title."""
    cleaned = _PAGE_SUFFIX_RE.sub("", title or "").strip()
    cleaned = _LEADING_NUMBER_RE.sub("", cleaned).strip()
    return cleaned


def extract_level1_titles(toc_hierarchies: list[dict[str, Any]]) -> list[str]:
    """Extract cleaned level-1 titles from ``toc_with_level`` payloads.

    TEXT-TRACK PDF shards attach calibrated TOC slices that only carry
    ``toc_with_level`` (no ``toc_tree``). Prefer that flat list so front-matter
    demotion keeps working after PROFILE skeleton reuse.
    """
    titles: list[str] = []
    for hier in toc_hierarchies:
        for entry in _iter_toc_with_level_entries(hier.get("toc_with_level")):
            level = entry.get("level")
            if level is None:
                continue
            try:
                if int(level) != 1:
                    continue
            except (TypeError, ValueError):
                continue
            heading = entry.get("heading") or entry.get("title") or ""
            cleaned = clean_toc_title(str(heading))
            if cleaned and len(cleaned) >= 2:
                titles.append(cleaned)
    return titles


def _iter_toc_with_level_entries(payload: Any) -> list[dict[str, Any]]:
    """Normalize ``toc_with_level`` to a list of entry dicts."""
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    return []


def find_first_body_boundary(
    lines: list[str],
    level1_titles: list[str],
) -> int | None:
    """Return the first line index matching a TOC level-1 title, if any."""
    normalized_titles = [
        normalize_match_text(title)
        for title in level1_titles
        if normalize_match_text(title)
    ]
    if not normalized_titles:
        return None

    for index, line in enumerate(lines):
        normalized_line = normalize_match_text(line.lstrip("#").strip())
        if any(title in normalized_line for title in normalized_titles):
            return index
    return None
