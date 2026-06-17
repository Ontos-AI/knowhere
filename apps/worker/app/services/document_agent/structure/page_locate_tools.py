"""Registry tools for page-memory residual title location.

These live under ``structure/`` (not ``tools/``) so the page-memory ReAct
sub-agent can register and dispatch them without importing the full profiling
``tools`` package (which pulls in S3/DB-heavy modules). ``tools/page_locate.py``
re-exports these so the profile executor and ``tools/__init__`` keep working.
"""

from __future__ import annotations

import time
from typing import Any

from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.registry import register_tool
from app.services.document_agent.structure import page_locate_agent as _pla
from app.services.document_agent.structure.hierarchy_locator import TitleMatch

SYNTHETIC_CANDIDATE_CONFIDENCE = 0.4


@register_tool(
    name="grep.title_pages",
    description=(
        "Find candidate body pages for a section title using strict heading, "
        "normalized, compact, and token grep variants."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "pages": {"type": "array", "items": {"type": "integer"}},
            "candidate_cap": {"type": "integer"},
        },
        "required": ["title", "pages"],
    },
)
def grep_title_pages(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    title = str(args.get("title") or "").strip()
    pages = _valid_pages(ctx, args.get("pages") or [])
    if not title or not pages:
        return ToolResult(
            status="error",
            error="grep.title_pages requires title and pages",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    page_texts = _get_page_texts(ctx, pages)
    matches = _pla.grep_title_page_candidates(
        title=title,
        scope_pages=pages,
        page_texts=page_texts,
        limit=int(args.get("candidate_cap") or 8),
    )
    return ToolResult(
        status="ok",
        payload={
            "title": title,
            "candidates": [_match_to_payload(match) for match in matches],
        },
        latency_ms=int((time.monotonic() - start) * 1000),
    )


@register_tool(
    name="verify.section_page",
    description=(
        "Use VLM page screenshots to choose which candidate page starts a section."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "pages": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["title", "pages"],
    },
)
def verify_section_page(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    title = str(args.get("title") or "").strip()
    pages = _valid_pages(ctx, args.get("pages") or [])
    if not title or not pages:
        return ToolResult(
            status="error",
            error="verify.section_page requires title and pages",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    page_texts = _get_page_texts(ctx, pages)
    grep_by_page = {
        match.page: match
        for match in _pla.grep_title_page_candidates(
            title=title,
            scope_pages=pages,
            page_texts=page_texts,
            limit=len(pages),
        )
    }
    # Always verify the *requested* pages, even when grep found nothing on them
    # (the heading may be split across lines / wrapped, so render + VLM decides).
    candidates = [
        grep_by_page.get(page)
        or TitleMatch(
            page=page,
            confidence=SYNTHETIC_CANDIDATE_CONFIDENCE,
            source="agent_heuristic",
            matched_line="",
            score=SYNTHETIC_CANDIDATE_CONFIDENCE,
            candidates=[page],
            evidence={"synthesized": True},
        )
        for page in pages
    ]
    choice = _pla.verify_section_page_choice(
        ctx=ctx,
        title=title,
        candidate_matches=candidates,
        candidate_page_cap=len(pages),
    )
    return ToolResult(
        status="ok",
        payload=choice,
        latency_ms=int((time.monotonic() - start) * 1000),
        tokens_used=int(choice.get("tokens_used") or 0),
    )


def _valid_pages(ctx: ToolContext, raw_pages: Any) -> list[int]:
    page_count = max(int(ctx.blackboard.page_count or 0), 0)
    return sorted(
        {
            int(page)
            for page in raw_pages
            if 1 <= int(page) <= page_count
        }
    )


def _get_page_texts(ctx: ToolContext, pages: list[int]) -> dict[int, str]:
    cache = getattr(ctx.blackboard, "page_full_text_cache", {})
    missing = [page for page in pages if page not in cache]
    if missing:
        from app.services.document_agent.pdf_text import read_page_texts

        cache.update(read_page_texts(ctx.pdf_path, missing, timeout=300))
    return {page: cache.get(page, "") for page in pages}


def _match_to_payload(match: Any) -> dict[str, Any]:
    return {
        "page": match.page,
        "confidence": match.confidence,
        "source": match.source,
        "matched_line": match.matched_line,
        "score": match.score,
        "candidates": match.candidates,
        "evidence": match.evidence,
    }
