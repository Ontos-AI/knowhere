"""Generic full-document text grep for native PDFs."""

from __future__ import annotations

import re
import time
from typing import Any

from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.registry import (
    has_page_features,
    has_page_full_text,
    not_is_scanned,
    register_tool,
)
from app.services.document_parser.structure.body_boundary import normalize_match_text


@register_tool(
    name="grep.text",
    description=(
        "Search normalized PDF text for a substring or regex. Whitespace is "
        "collapsed with CJK-aware spacing and matching is case-insensitive."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "regex": {"type": "boolean", "default": False},
            "max_results": {"type": "integer", "default": 30},
            "context_chars": {"type": "integer", "default": 80},
            "start_page": {"type": "integer"},
            "end_page": {"type": "integer"},
        },
        "required": ["query"],
    },
    preconditions=(has_page_features, has_page_full_text, not_is_scanned),
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
    max_results = max(1, min(int(args.get("max_results") or 30), 100))
    context_chars = max(20, min(int(args.get("context_chars") or 80), 300))
    start_page = max(1, int(args.get("start_page") or 1))
    end_page = int(args.get("end_page") or ctx.blackboard.page_count or 0)
    normalized_query = normalize_match_text(query)
    if not normalized_query:
        return ToolResult(
            status="error",
            error="grep.text normalized query is empty",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    pattern = re.compile(
        normalized_query if use_regex else re.escape(normalized_query)
    )
    results: list[dict[str, Any]] = []
    hit_count = 0
    hit_pages: list[int] = []
    for page, text in sorted(ctx.blackboard.page_full_text_cache.items()):
        if page < start_page or (end_page and page > end_page):
            continue
        normalized_text = normalize_match_text(text)
        page_hit = False
        for match in pattern.finditer(normalized_text):
            hit_count += 1
            page_hit = True
            if len(results) >= max_results:
                continue
            start_idx = max(match.start() - context_chars, 0)
            end_idx = min(match.end() + context_chars, len(normalized_text))
            results.append(
                {
                    "page": page,
                    "char_offset": match.start(),
                    "snippet": normalized_text[start_idx:end_idx],
                }
            )
        if page_hit:
            hit_pages.append(page)
    summary = {
        "query": query,
        "normalized_query": normalized_query,
        "hit_count": hit_count,
        "hit_page_count": len(hit_pages),
        "hit_pages": hit_pages,
        "results": results,
    }
    ctx.blackboard.global_signals.setdefault("grep_history", []).append(
        {
            "query": query,
            "normalized_query": normalized_query,
            "hit_count": hit_count,
            "hit_page_count": len(hit_pages),
            "sample_pages": hit_pages[:10],
        }
    )
    return ToolResult(
        status="ok",
        payload=summary,
        latency_ms=int((time.monotonic() - start) * 1000),
        output_summary={
            "query": query,
            "hit_count": hit_count,
            "hit_page_count": len(hit_pages),
        },
    )
