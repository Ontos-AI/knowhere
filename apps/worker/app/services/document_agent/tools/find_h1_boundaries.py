"""Find H1 starts from TOC candidates or heading-like body lines."""

from __future__ import annotations

import time
from typing import Any

from app.services.document_agent.heading_text import (
    candidate_allowed,
    clean_toc_line,
    fuzzy_match,
    looks_like_h1_line,
    normalize_heading,
)
from app.services.document_agent.manifest import H1BoundaryResult, H1Candidate, TocCandidate, ToolContext, ToolResult
from app.services.document_agent.pdf_text import meaningful_lines, read_page_texts
from app.services.document_agent.registry import register_tool
from app.services.document_agent.state import DocumentAgentState


def _all_non_toc_pages(ctx: ToolContext) -> list[int]:
    toc_pages = set(ctx.blackboard.toc_result.toc_pages if ctx.blackboard.toc_result else [])
    return [
        feature.page
        for feature in ctx.blackboard.page_features
        if feature.page not in toc_pages
    ]


def _fallback_candidates_from_previews(ctx: ToolContext) -> list[TocCandidate]:
    candidates: list[TocCandidate] = []
    seen: set[str] = set()
    for feature in ctx.blackboard.page_features:
        for line_index, line in enumerate(feature.text_lines_preview):
            cleaned = clean_toc_line(line)
            if not looks_like_h1_line(cleaned) or not candidate_allowed(cleaned):
                continue
            normalized = normalize_heading(cleaned)
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(
                TocCandidate(
                    title=cleaned,
                    normalized_title=normalized,
                    source_page=feature.page,
                    line_index=line_index,
                )
            )
    return candidates


def _match_candidate(
    candidate: TocCandidate,
    page_texts: dict[int, str],
) -> H1Candidate | None:
    for page, text in sorted(page_texts.items()):
        lines = meaningful_lines(text)
        for line_index, line in enumerate(lines[:20]):
            if candidate.title in line:
                return H1Candidate(
                    title=candidate.title,
                    page=page,
                    confidence=1.0,
                    matched_line=line,
                    source="toc_exact_top",
                    evidence={"line_index": line_index, "toc_page": candidate.source_page},
                )
            if fuzzy_match(candidate.normalized_title, line):
                return H1Candidate(
                    title=candidate.title,
                    page=page,
                    confidence=0.86,
                    matched_line=line,
                    source="toc_fuzzy_top",
                    evidence={"line_index": line_index, "toc_page": candidate.source_page},
                )
    return None


@register_tool(
    name="find.h1_boundaries",
    description="Match H1 candidates to body page starts with evidence and confidence.",
    allowed_states={DocumentAgentState.CLASSIFIED},
)
def find_h1_boundaries(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    toc_result = ctx.blackboard.toc_result
    candidates = list(toc_result.candidates) if toc_result and toc_result.candidates else []
    method: str = "toc_grep" if candidates else "heading_grep"
    if not candidates:
        candidates = _fallback_candidates_from_previews(ctx)
    pages = _all_non_toc_pages(ctx)
    page_texts = read_page_texts(ctx.pdf_path, pages) if pages else {}
    matches: list[H1Candidate] = []
    seen_pages_titles: set[tuple[int, str]] = set()
    for candidate in candidates:
        match = _match_candidate(candidate, page_texts)
        if match is None:
            continue
        key = (match.page, normalize_heading(match.title))
        if key in seen_pages_titles:
            continue
        seen_pages_titles.add(key)
        matches.append(match)
    if not matches:
        method = "none"
    result = H1BoundaryResult(
        h1_candidates=matches,
        method=method,  # type: ignore[arg-type]
        notes=f"{len(matches)} of {len(candidates)} candidates matched",
    )
    ctx.blackboard.h1_result = result
    ctx.blackboard.global_signals["h1_candidate_count"] = len(matches)
    return ToolResult(
        status="ok",
        payload={"h1_match_count": len(matches), "method": method},
        latency_ms=int((time.monotonic() - start) * 1000),
    )
