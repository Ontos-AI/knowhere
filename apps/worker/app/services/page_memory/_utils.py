"""Shared utilities for the page-memory track."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

_NATURAL_RE = re.compile(r"(\d+)")


def _natural_key(path: str) -> list[int | str]:
    """Split a string into a list of int/str segments for natural ordering."""
    return [int(c) if c.isdigit() else c.lower() for c in _NATURAL_RE.split(path)]


def sort_skeletons(skeletons: list[Any]) -> list[Any]:
    return sorted(
        skeletons,
        key=lambda item: (
            int(getattr(item, "start_page", 0) or 0),
            int(getattr(item, "level", 0) or 0),
            _natural_key(str(getattr(item, "section_path", "") or "")),
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


@dataclass(frozen=True)
class CoarseScope:
    scope_id: str
    skeletons: list[Any]
    strategy: str
    start_page: int
    end_page: int


def build_hierarchy_scopes(
    *,
    skeletons: list[Any],
    filename: str,
    page_count: int,
) -> list[CoarseScope]:
    """Split skeleton list into per-leaf scopes for independent processing.

    Each TOC leaf skeleton becomes its own scope. ``skeletons`` is leaf-level
    (produced by ``extract_section_skeletons`` → ``resolve_hierarchy_page_ranges``,
    which only emits leaf nodes). When multiple leaves share the same page range
    (e.g. unlocated leaves that fell back to the same parent scope), they are
    merged into a single scope to keep ``scope_id`` unique.
    """
    if not skeletons:
        return []

    root_fallback = all(
        getattr(item, "title", "") == "Root"
        or (getattr(item, "evidence", {}) or {}).get("source") == "fallback_root"
        for item in skeletons
    )
    if root_fallback:
        ordered = sort_skeletons(skeletons)
        return [
            CoarseScope(
                scope_id=scope_id_for_pages(1, page_count),
                skeletons=ordered,
                strategy="fallback_root",
                start_page=1,
                end_page=page_count,
            )
        ]

    scopes_by_range: dict[tuple[int, int], CoarseScope] = {}
    for skel in sort_skeletons(skeletons):
        start_page = max(1, int(getattr(skel, "start_page", 1) or 1))
        end_page = min(page_count, int(getattr(skel, "end_page", start_page) or start_page))
        if end_page < start_page:
            continue
        key = (start_page, end_page)
        existing = scopes_by_range.get(key)
        if existing is None:
            scopes_by_range[key] = CoarseScope(
                scope_id=scope_id_for_pages(start_page, end_page),
                skeletons=[skel],
                strategy="leaf_scope",
                start_page=start_page,
                end_page=end_page,
            )
        else:
            scopes_by_range[key] = replace(
                existing, skeletons=existing.skeletons + [skel]
            )
    return list(scopes_by_range.values())
