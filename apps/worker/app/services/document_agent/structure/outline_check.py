"""PDF outline self-check and compact tree digest for VLM confirm."""

from __future__ import annotations

import re
from typing import Any

from app.services.document_parser.structure.body_boundary import (
    clean_toc_title,
    normalize_heading_text,
)

_DEFAULT_DIGEST_TITLE_CHARS = 80


def flatten_outline_entries(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten nested outline roots into ``heading`` / ``level`` / ``page`` rows."""
    entries: list[dict[str, Any]] = []

    def walk(items: list[dict[str, Any]]) -> None:
        for node in items:
            title = str(node.get("title") or "").strip()
            if not title:
                continue
            level = node.get("level")
            try:
                level_i = int(level) if level is not None else 1
            except (TypeError, ValueError):
                level_i = 1
            page = node.get("page")
            entry: dict[str, Any] = {
                "heading": title,
                "level": level_i,
                "page": page if page is None else int(page),
            }
            entries.append(entry)
            children = node.get("children") or []
            if isinstance(children, list) and children:
                walk([child for child in children if isinstance(child, dict)])

    walk([node for node in nodes if isinstance(node, dict)])
    return entries


def verify_entries(
    entries: list[dict[str, Any]],
    page_texts: dict[int, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep entries whose title appears on the pointed physical page.

    Entries without a page (null-page parents) are kept without text checks.
    Failures drop only that entry; this is not a gate.
    """
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for entry in entries:
        page = entry.get("page")
        if page is None:
            kept.append(entry)
            continue
        try:
            page_i = int(page)
        except (TypeError, ValueError):
            dropped.append(entry)
            continue
        if _title_on_page(str(entry.get("heading") or ""), page_i, page_texts):
            kept.append(entry)
        else:
            dropped.append(entry)
    return kept, dropped


def build_tree_digest_from_entries(
    entries: list[dict[str, Any]],
    *,
    max_title_chars: int = _DEFAULT_DIGEST_TITLE_CHARS,
) -> str:
    """Compact outline digest for a single true/false VLM confirm call."""
    lines: list[str] = []
    for entry in entries:
        heading = str(entry.get("heading") or "").strip()
        if not heading:
            continue
        try:
            level = int(entry.get("level") or 1)
        except (TypeError, ValueError):
            level = 1
        indent = "  " * max(level - 1, 0)
        clipped = (
            heading if len(heading) <= max_title_chars else heading[:max_title_chars]
        )
        lines.append(f"{indent}L{level} {clipped}")
    return "\n".join(lines)


def _title_on_page(title: str, page: int, page_texts: dict[int, str]) -> bool:
    needle = _compact(clean_toc_title(title) or title)
    if not needle:
        return False
    haystack = _compact(page_texts.get(page, ""))
    return bool(haystack) and needle in haystack


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", normalize_heading_text(text)).casefold()
