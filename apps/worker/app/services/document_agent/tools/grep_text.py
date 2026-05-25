"""Generic full-document text grep for native PDFs."""

from __future__ import annotations

import re
import time
from typing import Any

from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.pdf_text import read_page_texts
from app.services.document_agent.registry import has_page_features, not_is_scanned, register_tool


def _load_page_texts(ctx: ToolContext) -> dict[int, str]:
    if ctx.blackboard.page_full_text_cache:
        return dict(ctx.blackboard.page_full_text_cache)
    pages = list(range(1, ctx.blackboard.page_count + 1))
    texts = read_page_texts(ctx.pdf_path, pages, timeout=300)
    ctx.blackboard.page_full_text_cache = texts
    return texts


@register_tool(
    name="grep.text",
    description="Search full PDF text for a substring or regex. Available only for native PDFs.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "regex": {"type": "boolean", "default": False},
            "case_sensitive": {"type": "boolean", "default": False},
            "max_results": {"type": "integer", "default": 30},
            "context_chars": {"type": "integer", "default": 80},
        },
        "required": ["query"],
    },
    preconditions=(has_page_features, not_is_scanned),
)
def grep_text(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(
            status="error",
            error="grep.text requires query",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    use_regex = bool(args.get("regex", False))
    case_sensitive = bool(args.get("case_sensitive", False))
    max_results = max(1, min(int(args.get("max_results") or 30), 100))
    context_chars = max(20, min(int(args.get("context_chars") or 80), 300))
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(query if use_regex else re.escape(query), flags)
    results: list[dict[str, Any]] = []
    for page, text in sorted(_load_page_texts(ctx).items()):
        for match in pattern.finditer(text):
            start_idx = max(match.start() - context_chars, 0)
            end_idx = min(match.end() + context_chars, len(text))
            results.append(
                {
                    "page": page,
                    "char_offset": match.start(),
                    "snippet": text[start_idx:end_idx].replace("\n", " "),
                }
            )
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break
    summary = {"query": query, "hit_count": len(results), "results": results}
    ctx.blackboard.global_signals.setdefault("grep_history", []).append(
        {"query": query, "hit_count": len(results), "sample_pages": [item["page"] for item in results[:10]]}
    )
    return ToolResult(
        status="ok",
        payload=summary,
        latency_ms=int((time.monotonic() - start) * 1000),
        output_summary={"query": query, "hit_count": len(results)},
    )
