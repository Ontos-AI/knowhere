"""judge.toc_source: pick between the PDF outline tree and printed TOC page text.

Text-only comparison over ``page_full_text_cache``. Coverage decides;
granularity only breaks ties.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any, cast

from loguru import logger

from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.pdf_text import page_content_map
from app.services.document_agent.registry import has_page_full_text, register_tool

OUTLINE_CHOICE = "outline"
PRINTED_TOC_CHOICE = "printed_toc"

_INSTRUCTIONS = (
    "Two candidate sources describe the section structure of the same file.\n"
    "Source 'outline' is the bookmark tree as level-prefixed lines.\n"
    "Source 'printed_toc' is table-of-contents text extracted from places "
    "across this document; it is TOC content only, not body text.\n"
    "Choose which source covers more of the document.\n"
    "Rank by coverage first: how many major sections/chapters a source lists "
    "across the whole document.\n"
    "Example: if outline lists only a few chapters in depth, while printed_toc "
    "lists all chapters, choose printed_toc, since the printed_toc covers more content.\n"
    
    "Only when coverage is comparable, prefer the finer-grained source "
    "(more depth levels and more entries).\n"
    'Return a strict json object with keys {"choice": "outline" or '
    '"printed_toc", "reason": string}. Include the word json.'
)


def merge_printed_toc_texts(page_texts: list[str]) -> str:
    """Drop lines shared by two or more TOC pages; join remaining lines in order."""
    pages_lines: list[list[str]] = []
    for text in page_texts:
        lines = [line.strip() for line in str(text).splitlines() if line.strip()]
        if lines:
            pages_lines.append(lines)
    if not pages_lines:
        return ""
    if len(pages_lines) == 1:
        return "\n".join(pages_lines[0])

    presence: Counter[str] = Counter()
    for lines in pages_lines:
        for line in set(lines):
            presence[line] += 1
    boilerplate = {line for line, count in presence.items() if count >= 2}

    kept: list[str] = []
    for lines in pages_lines:
        for line in lines:
            if line not in boilerplate:
                kept.append(line)
    return "\n".join(kept)


@register_tool(
    name="judge.toc_source",
    description=(
        "Compare a PDF outline tree against printed TOC page text and choose "
        "the source with broader coverage (finer granularity breaks ties)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "outline_digest": {
                "type": "string",
                "description": "Level-prefixed outline tree digest",
            },
            "toc_pages": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "1-based physical pages holding the printed TOC",
            },
        },
        "required": ["outline_digest", "toc_pages"],
    },
    preconditions=(has_page_full_text,),
)
def judge_toc_source(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    outline_digest = str(args.get("outline_digest") or "").strip()
    raw_pages = args.get("toc_pages") or []
    if not outline_digest:
        return ToolResult(
            status="error",
            error="judge.toc_source requires outline_digest",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    if not isinstance(raw_pages, list) or not raw_pages:
        return ToolResult(
            status="error",
            error="judge.toc_source requires toc_pages[]",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    pages: list[int] = []
    for item in raw_pages:
        try:
            page = int(item)
        except (TypeError, ValueError):
            continue
        if page not in pages:
            pages.append(page)
    if not pages:
        return ToolResult(
            status="error",
            error="judge.toc_source has no valid toc_pages",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    cache = page_content_map(ctx.blackboard.page_full_text_cache)
    printed_toc = merge_printed_toc_texts([cache.get(page, "") for page in pages])
    if not printed_toc.strip():
        return ToolResult(
            status="error",
            error="judge.toc_source printed_toc text empty",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    prompt = (
        f"{_INSTRUCTIONS}\n\n"
        f"Source outline:\n{outline_digest}\n\n"
        f"Source printed_toc:\n{printed_toc}\n"
    )

    try:
        from shared.services.ai.llm_overrides import get_text_client

        client, model = get_text_client()
        raw, usage = client.chat_completion_with_usage(
            messages=cast(Any, [{"role": "user", "content": prompt}]),
            model=model,
            temperature=0.0,
            max_tokens=400,
            response_format={"type": "json_object"},
            usage_task="document_agent.judge_toc_source",
        )
        payload = json.loads(raw) if raw else {}
    except Exception as exc:
        logger.warning("[judge.toc_source] LLM failed: {}", exc)
        return ToolResult(
            status="error",
            error=f"llm failed: {exc}",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    choice = str(payload.get("choice") or "").strip().lower()
    if choice not in {OUTLINE_CHOICE, PRINTED_TOC_CHOICE}:
        return ToolResult(
            status="error",
            error=f"judge.toc_source returned unknown choice: {choice!r}",
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    reason = str(payload.get("reason") or "")
    tokens_used = int((usage or {}).get("total_tokens") or 0)
    logger.info(
        "[judge.toc_source] choice={} toc_pages={} reason={}",
        choice,
        pages,
        reason,
    )
    return ToolResult(
        status="ok",
        payload={"choice": choice, "reason": reason, "toc_pages": pages},
        latency_ms=int((time.monotonic() - start) * 1000),
        tokens_used=tokens_used,
        output_summary={"choice": choice, "toc_page_count": len(pages)},
    )
