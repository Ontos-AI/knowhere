"""Match TOC level-1 headings to body pages via PyMuPDF text search."""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, cast

from app.services.document_agent.manifest import (
    H1BoundaryResult,
    H1Candidate,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.pdf_text import read_page_texts
from app.services.document_agent.registry import has_toc_result, register_tool
from app.services.document_agent.visual import render_pages
from app.services.document_parser.structure.body_boundary import (
    clean_toc_title,
    extract_level1_titles,
    normalize_heading_text,
)
from loguru import logger


# ── C1: Unified grep matching ────────────────────────────────────────────


def grep_titles_in_pages(
    titles: list[str],
    search_pages: list[int],
    page_texts: dict[int, str],
    *,
    source: str = "toc_grep",
    confidence: float = 0.88,
) -> tuple[list[H1Candidate], list[str]]:
    """Grep a list of titles across specified pages, returning match results.

    H1/H2 share this function.  Callers control scope via *titles* and
    *search_pages*.

    Returns:
        (matched_candidates, unmatched_titles)
    """
    candidates: list[H1Candidate] = []
    unmatched: list[str] = []

    for title in titles:
        normalized_title = normalize_heading_text(title)
        found = False
        for page in search_pages:
            text = page_texts.get(page, "")
            if normalized_title in normalize_heading_text(text):
                matched_line = ""
                for line in text.splitlines():
                    if normalized_title in normalize_heading_text(line):
                        matched_line = line.strip()[:100]
                        break
                candidates.append(
                    H1Candidate(
                        title=title,
                        page=page,
                        confidence=confidence,
                        matched_line=matched_line,
                        source=source,  # type: ignore[arg-type]
                        evidence={
                            "normalized_needle": normalized_title,
                            "page_text_length": len(text),
                        },
                    )
                )
                found = True
                break  # First match per title
        if not found:
            unmatched.append(title)

    # Deduplicate by page – keep first hit
    seen: set[int] = set()
    deduped: list[H1Candidate] = []
    for c in candidates:
        if c.page not in seen:
            seen.add(c.page)
            deduped.append(c)

    return deduped, unmatched


def extract_children_titles(
    toc_hierarchies: list[dict[str, Any]],
    parent_title: str,
) -> list[str]:
    """Extract level-2 titles under a given H1 parent from toc_with_level."""
    titles: list[str] = []
    for hier in toc_hierarchies or []:
        entries = hier.get("toc_with_level", [])
        in_scope = False
        for entry in entries:
            if entry.get("level") == 1:
                cleaned = clean_toc_title(entry.get("heading", ""))
                in_scope = normalize_heading_text(cleaned) == normalize_heading_text(
                    parent_title
                )
                continue
            if in_scope and entry.get("level") == 2:
                cleaned = clean_toc_title(entry.get("heading", ""))
                if cleaned and len(cleaned) >= 2:
                    titles.append(cleaned)
    return titles


def _extract_level1_titles(toc_hierarchies: list[dict[str, Any]]) -> list[str]:
    return extract_level1_titles(toc_hierarchies)


# ── C2: Lazy VLM verification ────────────────────────────────────────────


def verify_section_start(
    *,
    page: int,
    title: str,
    ctx: ToolContext,
) -> bool:
    """VLM-confirm whether *page* is the start of a section titled *title*.

    Used for lazy verification before committing a shard cut.
    If VLM is unavailable (no model / budget exhausted / render fails),
    returns ``True`` (trust GREP).
    """
    model = ctx.settings.get("vlm_model") or os.environ.get("IMAGE_MODEL")
    if not model:
        return True  # No VLM → trust GREP

    # Render 1 page PNG
    png_items = render_pages(
        ctx, [page], folder_name="verify_pages", prefix="verify", timeout=60,
    )
    if not png_items:
        return True  # Render failed → trust GREP

    prompt = (
        f"This is page {page} of a PDF document.\n"
        f"Question: Is this page the START of a section titled '{title}'?\n"
        "Criteria: The title appears as a prominent heading/title on this page, "
        "not merely mentioned in body text.\n"
        'Return JSON: {"is_section_start": true/false, "reason": "brief"}'
    )
    est = 800  # ~800 tokens for 1 image
    if not ctx.budget.try_reserve("visual", est):
        return True  # Budget exhausted → trust GREP

    try:
        png_path = str(png_items[0]["png_path"])
        with open(png_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        content_parts: list[dict[str, Any]] = [
            {"type": "text", "text": prompt},
            {"type": "text", "text": f"\n--- Page {page} ---"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"},
            },
        ]

        from shared.services.ai.openai_compatible_client_sync import (
            get_openai_client,
        )

        client = get_openai_client(model=model)
        raw, usage = client.chat_completion_with_usage(
            messages=cast(Any, [{"role": "user", "content": content_parts}]),
            model=model,
            temperature=0.0,
            max_tokens=256,
            response_format={"type": "json_object"},
        )
        ctx.budget.commit(
            "visual", actual=usage.get("total_tokens", est), est=est,
        )
        data = json.loads(raw)
        result = bool(data.get("is_section_start", True))
        logger.info(
            "[verify_section_start] page={} title='{}' → {} reason={}",
            page, title[:30], result, data.get("reason", ""),
        )
        return result
    except Exception as exc:
        ctx.budget.refund("visual", est=est)
        logger.warning("[verify_section_start] VLM failed for page {}: {}", page, exc)
        return True  # VLM failure → trust GREP


# ── Tool registration ────────────────────────────────────────────────────


@register_tool(
    name="match.h1_pages",
    description=(
        "Match TOC level-1 headings to body pages using PyMuPDF substring search. "
        "Produces H1Candidate list for downstream shard planning."
    ),
    preconditions=(has_toc_result,),
)
def match_h1_pages(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()

    if not ctx.blackboard.toc_hierarchies:
        logger.info("[match.h1_pages] no toc_hierarchies, skipping")
        ctx.blackboard.h1_result = H1BoundaryResult(
            method="none",
            notes="No toc_hierarchies available for H1 matching",
        )
        return ToolResult(
            status="ok",
            payload={"h1_count": 0},
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    level1_titles = _extract_level1_titles(ctx.blackboard.toc_hierarchies)
    if not level1_titles:
        logger.info("[match.h1_pages] no level-1 titles in toc_hierarchies")
        ctx.blackboard.h1_result = H1BoundaryResult(
            method="toc_grep",
            notes="toc_hierarchies contained no level-1 entries",
        )
        return ToolResult(
            status="ok",
            payload={"h1_count": 0},
            latency_ms=int((time.monotonic() - start) * 1000),
            output_summary={"level1_titles": level1_titles},
        )

    # Build exclusion set: TOC pages should not be searched
    toc_page_set: set[int] = set()
    if ctx.blackboard.toc_result:
        toc_page_set.update(ctx.blackboard.toc_result.toc_pages)

    # Read text for all non-TOC pages
    search_pages = sorted(
        p
        for p in range(1, ctx.blackboard.page_count + 1)
        if p not in toc_page_set
    )
    page_texts = read_page_texts(ctx.pdf_path, search_pages, timeout=300)

    # Delegate to unified grep function
    h1_candidates, unmatched_titles = grep_titles_in_pages(
        level1_titles, search_pages, page_texts, source="toc_exact_top",
    )

    matched_titles = [c.title for c in h1_candidates]

    ctx.blackboard.h1_result = H1BoundaryResult(
        h1_candidates=h1_candidates,
        method="toc_grep",
        notes=(
            f"Matched {len(matched_titles)}/{len(level1_titles)} level-1 titles. "
            f"Unmatched: {unmatched_titles[:5]}"
        ),
    )

    logger.info(
        "[match.h1_pages] matched {}/{} level-1 titles to body pages: {}",
        len(matched_titles),
        len(level1_titles),
        [(c.title[:20], c.page) for c in h1_candidates],
    )

    return ToolResult(
        status="ok",
        payload={"h1_count": len(h1_candidates)},
        latency_ms=int((time.monotonic() - start) * 1000),
        output_summary={
            "level1_titles": level1_titles,
            "matched": [(c.title, c.page) for c in h1_candidates],
            "unmatched": unmatched_titles,
        },
    )
