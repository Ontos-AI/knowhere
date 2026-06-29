"""Shared utilities for the page-memory track."""

from __future__ import annotations

from typing import Any


def sort_skeletons(skeletons: list[Any]) -> list[Any]:
    return sorted(
        skeletons,
        key=lambda item: (
            int(getattr(item, "start_page", 0) or 0),
            int(getattr(item, "level", 0) or 0),
            str(getattr(item, "section_path", "") or ""),
        ),
    )


def scope_id_for_pages(start_page: int, end_page: int) -> str:
    return f"p{max(1, int(start_page))}-{max(1, int(end_page))}"


def collapse_page_ranges(pages: list[int]) -> list[list[int]]:
    if not pages:
        return []
    ranges: list[list[int]] = []
    start = prev = pages[0]
    for page in pages[1:]:
        if page == prev + 1:
            prev = page
            continue
        ranges.append([start, prev])
        start = prev = page
    ranges.append([start, prev])
    return ranges


def page_scope_info(pages: Any) -> dict[str, Any]:
    normalized: list[int] = []
    for raw_page in pages or []:
        try:
            page = int(raw_page)
        except (TypeError, ValueError):
            continue
        if page > 0:
            normalized.append(page)
    normalized = sorted(set(normalized))
    return {
        "page_count": len(normalized),
        "page_ranges": collapse_page_ranges(normalized),
    }
