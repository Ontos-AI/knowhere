"""Monotonic leaf-to-rendered-page attribution for DOCX final chunks."""

from __future__ import annotations

import unicodedata

from app.services.document_parser.structure.body_boundary import normalize_match_text


def attribute_pages_to_leaves(
    leaf_texts_in_order: list[str],
    page_texts: dict[int, str] | list[str],
) -> dict[int, list[int]]:
    """Map each leaf index to the rendered pages it occupies.

    The cursor only moves forward. A leaf that cannot be located keeps an
    empty page list and does not rewind the cursor.
    """
    numbered_pages = _numbered_pages(page_texts)
    haystack, page_ends, page_numbers = _build_haystack(numbered_pages)
    cursor = 0
    attributed: dict[int, list[int]] = {}
    for leaf_index, leaf_text in enumerate(leaf_texts_in_order):
        span = _locate_leaf(str(leaf_text or ""), haystack, cursor)
        if span is None:
            attributed[leaf_index] = []
            continue
        start, end = span
        attributed[leaf_index] = _pages_for_span(start, end, page_ends, page_numbers)
        cursor = end
    return attributed


def _numbered_pages(
    page_texts: dict[int, str] | list[str],
) -> list[tuple[int, str]]:
    if isinstance(page_texts, dict):
        return [
            (int(page), str(text or ""))
            for page, text in sorted(page_texts.items(), key=lambda item: int(item[0]))
            if int(page) > 0
        ]
    return [
        (index + 1, str(text or ""))
        for index, text in enumerate(page_texts)
    ]


def _build_haystack(
    numbered_pages: list[tuple[int, str]],
) -> tuple[str, list[int], list[int]]:
    pieces: list[str] = []
    page_ends: list[int] = []
    page_numbers: list[int] = []
    cursor = 0
    for page, text in numbered_pages:
        normalized = _normalize_attr_text(text)
        cursor += len(normalized)
        pieces.append(normalized)
        page_ends.append(cursor)
        page_numbers.append(page)
    return "".join(pieces), page_ends, page_numbers


def _locate_leaf(leaf_text: str, haystack: str, cursor: int) -> tuple[int, int] | None:
    remaining = haystack[cursor:]
    if not remaining:
        return None
    needle = _normalize_attr_text(leaf_text)
    if needle:
        found = remaining.find(needle)
        if found >= 0:
            start = cursor + found
            return start, start + len(needle)
    tokens = [token for token in normalize_match_text(leaf_text).split() if len(token) >= 2]
    if not tokens:
        return None
    search_at = 0
    start: int | None = None
    end: int | None = None
    for token in tokens:
        found = remaining.find(token, search_at)
        if found < 0:
            return None
        if start is None:
            start = cursor + found
        end = cursor + found + len(token)
        search_at = found + len(token)
    if start is None or end is None:
        return None
    return start, end


def _pages_for_span(
    start: int,
    end: int,
    page_ends: list[int],
    page_numbers: list[int],
) -> list[int]:
    if end <= start or not page_ends:
        return []
    last_index = end - 1
    pages: list[int] = []
    span_start = 0
    for page_end, page in zip(page_ends, page_numbers, strict=True):
        if page_end <= start:
            span_start = page_end
            continue
        if span_start > last_index:
            break
        pages.append(page)
        if page_end > last_index:
            break
        span_start = page_end
    return pages


def _normalize_attr_text(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text or "").casefold().split())
