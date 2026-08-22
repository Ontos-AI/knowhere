"""Generic full-document text grep for native PDFs."""

from __future__ import annotations

import re
import time
from typing import Any

from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.pdf_text import page_content_map
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
        "Search normalized PDF text for a substring, regex, or complete line. "
        "Whitespace is collapsed with CJK-aware spacing and matching is "
        "case-insensitive. Uses page_text_search_view when set (after "
        "text.strip_*), else each page's stored content field."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "regex": {"type": "boolean", "default": False},
            "whole_line": {"type": "boolean", "default": False},
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
    whole_line = bool(args.get("whole_line", False))
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
    view = ctx.blackboard.page_text_search_view
    if view is None:
        texts = page_content_map(ctx.blackboard.page_full_text_cache)
    else:
        texts = {int(page): str(text) for page, text in view.items()}
    results: list[dict[str, Any]] = []
    hit_count = 0
    hit_pages: list[int] = []
    for page, text in sorted(texts.items()):
        if page < start_page or (end_page and page > end_page):
            continue
        page_hit = False
        if whole_line:
            for line_index, line in enumerate(str(text or "").splitlines()):
                normalized_line = normalize_match_text(line)
                if not normalized_line or pattern.fullmatch(normalized_line) is None:
                    continue
                hit_count += 1
                page_hit = True
                if len(results) < max_results:
                    results.append(
                        {
                            "page": page,
                            "line_index": line_index,
                            "char_offset": 0,
                            "snippet": normalized_line,
                        }
                    )
        else:
            normalized_text = normalize_match_text(str(text or ""))
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
        "whole_line": whole_line,
        "hit_count": hit_count,
        "hit_page_count": len(hit_pages),
        "hit_pages": hit_pages,
        "results": results,
    }
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
