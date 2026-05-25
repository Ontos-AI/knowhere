"""Match TOC level-1 headings to body pages via PyMuPDF text search."""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Any

from app.services.document_agent.manifest import (
    H1BoundaryResult,
    H1Candidate,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.pdf_text import read_page_texts
from app.services.document_agent.registry import has_toc_result, register_tool
from loguru import logger


# ── Text normalization for matching ──────────────────────────────────────

_LEADING_NUMBER_RE = re.compile(
    r"""^
    (?:
        [#]+\s*
        | 第\s*[零一二三四五六七八九十百千\d]+\s*[章节篇部分]
        | [零一二三四五六七八九十百千]+\s*[、。，,]
        | [（(]\s*[零一二三四五六七八九十百千\d]+\s*[）)]
        | \d+(?:\.\d+)*\.?\s*
        | [IVXLCDM]+\.?\s+
        | [A-Za-z]\.\s+
        | Chapter\s+\w+\s*
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

_PAGE_SUFFIX_RE = re.compile(r"[\s\.\-·…]+\d+\s*$")


def _normalize(text: str) -> str:
    """Normalize text for fuzzy heading matching."""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _clean_toc_title(title: str) -> str:
    """Remove leading numbering/hashes and trailing page numbers from a TOC title."""
    cleaned = _PAGE_SUFFIX_RE.sub("", title or "").strip()
    cleaned = _LEADING_NUMBER_RE.sub("", cleaned).strip()
    return cleaned


def _extract_level1_titles(toc_hierarchies: list[dict[str, Any]]) -> list[str]:
    """Extract level-1 titles from toc_hierarchies.

    Each hierarchy dict contains ``toc_tree`` – a nested dict where top-level
    keys are level-1 headings (values are sub-heading dicts).
    """
    titles: list[str] = []
    for hier in toc_hierarchies:
        toc_tree = hier.get("toc_tree") or {}
        for raw_title in toc_tree.keys():
            cleaned = _clean_toc_title(raw_title)
            if cleaned and len(cleaned) >= 2:
                titles.append(cleaned)
    return titles


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

    # Strict substring matching: for each level-1 title, find the first body page
    h1_candidates: list[H1Candidate] = []
    matched_titles: list[str] = []
    unmatched_titles: list[str] = []

    for title in level1_titles:
        normalized_title = _normalize(title)
        found = False
        for page in search_pages:
            text = page_texts.get(page, "")
            normalized_text = _normalize(text)
            if normalized_title in normalized_text:
                # Find the matched line for evidence
                matched_line = ""
                for line in text.splitlines():
                    if normalized_title in _normalize(line):
                        matched_line = line.strip()[:100]
                        break

                h1_candidates.append(
                    H1Candidate(
                        title=title,
                        page=page,
                        confidence=0.88,
                        matched_line=matched_line,
                        source="toc_exact_top",
                        evidence={
                            "normalized_needle": normalized_title,
                            "page_text_length": len(text),
                        },
                    )
                )
                matched_titles.append(title)
                found = True
                break  # Only first match per title

        if not found:
            unmatched_titles.append(title)

    # Deduplicate: if multiple titles map to the same page, keep the first
    seen_pages: set[int] = set()
    deduped: list[H1Candidate] = []
    for candidate in h1_candidates:
        if candidate.page not in seen_pages:
            seen_pages.add(candidate.page)
            deduped.append(candidate)
    h1_candidates = deduped

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
