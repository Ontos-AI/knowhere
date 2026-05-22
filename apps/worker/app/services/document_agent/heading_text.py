"""Heading extraction and matching helpers."""

from __future__ import annotations

import re
import unicodedata


LEADING_NUMBER_RE = re.compile(
    r"""^
    (?:
        第\s*[零一二三四五六七八九十百千\d]+\s*[章节篇部分]
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

H1_LINE_RE = re.compile(
    r"""^\s*
    (?:
        第\s*[零一二三四五六七八九十百千\d]+\s*[章篇部]
        | [零一二三四五六七八九十百千]+\s*[、。]
        | \d+\s*[\.\s]
        | Chapter\s+\w+
        | [IVXLCDM]+\.?\s+\w
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

SUBHEADING_RE = re.compile(r"^\s*\d+\.\d+", re.IGNORECASE)
PAGE_SUFFIX_RE = re.compile(r"[\s\.\-·…]+\d+\s*$")


def normalize_heading(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    stripped = LEADING_NUMBER_RE.sub("", text).strip()
    return stripped if stripped else text


def has_numbering(text: str) -> bool:
    return bool(LEADING_NUMBER_RE.match(text or ""))


def clean_toc_line(line: str) -> str:
    return PAGE_SUFFIX_RE.sub("", line or "").strip()


def looks_like_h1_line(line: str) -> bool:
    stripped = (line or "").strip()
    if not stripped or SUBHEADING_RE.match(stripped):
        return False
    return bool(H1_LINE_RE.match(stripped))


def candidate_allowed(title: str) -> bool:
    normalized = normalize_heading(title)
    if not normalized:
        return False
    if len(normalized) < 4 and not has_numbering(title):
        return False
    return True


def fuzzy_match(needle: str, haystack: str) -> bool:
    if not needle:
        return False
    normalized_haystack = re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", haystack or ""),
    )
    if needle in normalized_haystack:
        return True
    stripped = normalize_heading(needle)
    return bool(stripped and len(stripped) >= 4 and stripped in normalized_haystack)
