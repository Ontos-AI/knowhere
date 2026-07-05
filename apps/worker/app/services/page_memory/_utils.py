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
    """Split skeleton list into coarse scopes for independent processing.

    Groups skeletons by top-level section_path prefix. Skeletons not claimed
    by any top-node are grouped by their root ancestor (first path segment
    after filename) as orphan scopes.
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

    min_level = min(int(getattr(item, "level", 0) or 0) for item in skeletons)
    top_nodes = [
        item
        for item in skeletons
        if int(getattr(item, "level", 0) or 0) == min_level
        or not str(getattr(item, "parent_path", "") or "").startswith(f"{filename}/")
    ]
    seen_top_paths: set[str] = set()
    unique_top_nodes: list[Any] = []
    for item in sort_skeletons(top_nodes):
        if item.section_path in seen_top_paths:
            continue
        seen_top_paths.add(item.section_path)
        unique_top_nodes.append(item)

    scopes: list[CoarseScope] = []
    for index, top in enumerate(unique_top_nodes):
        prefix = f"{top.section_path}/"
        members = [
            item
            for item in skeletons
            if item.section_path == top.section_path
            or str(item.section_path).startswith(prefix)
        ]
        if not members:
            continue
        start_page = max(1, int(getattr(top, "start_page", 1) or 1))
        next_top_start = (
            int(getattr(unique_top_nodes[index + 1], "start_page", page_count + 1) or page_count + 1)
            if index + 1 < len(unique_top_nodes)
            else page_count + 1
        )
        end_page = min(
            page_count,
            max(
                start_page,
                min(
                    int(getattr(top, "end_page", page_count) or page_count),
                    next_top_start - 1,
                ),
            ),
        )
        bounded_members = [
            replace(
                item,
                start_page=max(
                    start_page,
                    int(getattr(item, "start_page", start_page) or start_page),
                ),
                end_page=min(
                    end_page,
                    int(getattr(item, "end_page", end_page) or end_page),
                ),
            )
            for item in members
            if int(getattr(item, "start_page", 1) or 1) <= end_page
            and int(getattr(item, "end_page", end_page) or end_page) >= start_page
        ]
        bounded_members = sort_skeletons(bounded_members)
        scopes.append(
            CoarseScope(
                scope_id=scope_id_for_pages(start_page, end_page),
                skeletons=bounded_members,
                strategy=f"coarse_scope_{index + 1}",
                start_page=start_page,
                end_page=end_page,
            )
        )

    if scopes:
        claimed_paths: set[str] = set()
        for scope in scopes:
            for item in scope.skeletons:
                claimed_paths.add(item.section_path)
        orphans = [item for item in skeletons if item.section_path not in claimed_paths]
        if orphans:
            root_groups: dict[str, list[Any]] = {}
            for item in orphans:
                parts = str(getattr(item, "section_path", "") or "").split("/")
                root_key = "/".join(parts[:2]) if len(parts) >= 2 else item.section_path
                root_groups.setdefault(root_key, []).append(item)
            for root_key, group in sorted(root_groups.items()):
                group = sort_skeletons(group)
                sp = max(1, min(int(getattr(g, "start_page", 1) or 1) for g in group))
                ep = min(page_count, max(int(getattr(g, "end_page", page_count) or page_count) for g in group))
                scopes.append(
                    CoarseScope(
                        scope_id=scope_id_for_pages(sp, ep),
                        skeletons=group,
                        strategy="orphan_scope",
                        start_page=sp,
                        end_page=ep,
                    )
                )
        return scopes

    ordered = sort_skeletons(skeletons)
    all_pages: set[int] = set()
    for item in ordered:
        sp = max(1, int(getattr(item, "start_page", 1) or 1))
        ep = min(page_count, int(getattr(item, "end_page", sp) or sp))
        if ep >= sp:
            all_pages.update(range(sp, ep + 1))
    pages = sorted(all_pages) or list(range(1, page_count + 1))
    return [
        CoarseScope(
            scope_id=scope_id_for_pages(
                pages[0] if pages else 1,
                pages[-1] if pages else page_count,
            ),
            skeletons=ordered,
            strategy="full_coarse_hierarchy",
            start_page=pages[0] if pages else 1,
            end_page=pages[-1] if pages else page_count,
        )
    ]
