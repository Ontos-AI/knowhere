"""probe.outline: read PDF bookmarks via get_toc and build a nested tree."""

from __future__ import annotations

import time
from typing import Any

from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.registry import has_page_features, register_tool


def _normalize_page(raw: Any) -> int | None:
    """PyMuPDF outline page is 1-based; ``<= 0`` means no destination page."""
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


def build_outline_forest(entries: list[list[Any]]) -> list[dict[str, Any]]:
    """Convert flat ``[level, title, page]`` rows into a nested forest.

    No-page destinations (``page <= 0``) become ``page=None`` and are retained;
    printed TOC likewise keeps entries whose printed pages are out of range.
    """
    roots: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    for row in entries:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        level = int(row[0])
        title = str(row[1] or "").strip()
        if not title or level < 1:
            continue
        node: dict[str, Any] = {
            "title": title,
            "level": level,
            "page": _normalize_page(row[2]),
            "children": [],
        }
        while stack and int(stack[-1]["level"]) >= level:
            stack.pop()
        if stack:
            stack[-1]["children"].append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots


def _count_nodes(nodes: list[dict[str, Any]]) -> int:
    total = 0
    for node in nodes:
        total += 1 + _count_nodes(list(node.get("children") or []))
    return total


@register_tool(
    name="probe.outline",
    description=(
        "Read PDF bookmark outline via get_toc(simple=True) and return a nested "
        "tree. No-page destinations are kept as page=null."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
    preconditions=(has_page_features,),
)
def probe_outline(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args  # no parameters
    start = time.monotonic()
    import fitz

    try:
        doc = fitz.open(ctx.pdf_path)
        try:
            raw = doc.get_toc(simple=True) or []
            page_count = int(doc.page_count)
        finally:
            doc.close()
    except Exception as exc:
        ctx.blackboard.pdf_outline_roots = []
        return ToolResult(
            status="ok",
            payload={
                "source": "pdf_outline",
                "page_count": 0,
                "raw_entry_count": 0,
                "node_count": 0,
                "roots": [],
                "error": f"probe.outline failed: {exc}",
            },
            latency_ms=int((time.monotonic() - start) * 1000),
            output_summary={"raw_entry_count": 0, "node_count": 0, "root_count": 0},
        )

    entries = [list(row) for row in raw if isinstance(row, (list, tuple))]
    forest = build_outline_forest(entries)
    ctx.blackboard.pdf_outline_roots = forest
    return ToolResult(
        status="ok",
        payload={
            "source": "pdf_outline",
            "page_count": page_count,
            "raw_entry_count": len(entries),
            "node_count": _count_nodes(forest),
            "roots": forest,
        },
        latency_ms=int((time.monotonic() - start) * 1000),
        output_summary={
            "raw_entry_count": len(entries),
            "node_count": _count_nodes(forest),
            "root_count": len(forest),
        },
    )
