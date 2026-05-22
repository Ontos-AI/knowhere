"""TOC page and level-1 candidate extraction."""

from __future__ import annotations

import time
from typing import Any

from app.services.document_agent.heading_text import (
    candidate_allowed,
    clean_toc_line,
    normalize_heading,
    looks_like_h1_line,
)
from app.services.document_agent.manifest import TocCandidate, TocResult, ToolContext, ToolResult
from app.services.document_agent.pdf_text import read_page_texts
from app.services.document_agent.registry import register_tool
from app.services.document_agent.state import DocumentAgentState


def _is_toc_feature_lines(lines: list[str]) -> bool:
    compact = "".join(lines).replace(" ", "").lower()
    return any(marker in compact for marker in ("目录", "目次", "contents", "tableofcontents"))


def _extract_candidates(page: int, text: str) -> list[TocCandidate]:
    candidates: list[TocCandidate] = []
    seen: set[str] = set()
    for idx, raw_line in enumerate(text.splitlines()):
        line = clean_toc_line(raw_line)
        if not looks_like_h1_line(line):
            continue
        if not candidate_allowed(line):
            continue
        normalized = normalize_heading(line)
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(
            TocCandidate(
                title=line,
                normalized_title=normalized,
                source_page=page,
                line_index=idx,
                numbering=line[: max(len(line) - len(normalized), 0)].strip(),
            )
        )
    return candidates


@register_tool(
    name="find.toc_pages",
    description="Find table-of-contents pages and extract level-1 heading candidates.",
    allowed_states={DocumentAgentState.PROBED},
)
def find_toc_pages(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    toc_pages = [
        feature.page
        for feature in ctx.blackboard.page_features
        if _is_toc_feature_lines(feature.text_lines_preview)
    ]
    texts = read_page_texts(ctx.pdf_path, toc_pages) if toc_pages else {}
    candidates: list[TocCandidate] = []
    for page, text in texts.items():
        candidates.extend(_extract_candidates(page, text))
    result = TocResult(
        toc_pages=sorted(toc_pages),
        candidates=candidates,
        method="toc_marker" if toc_pages else "none",
        notes=f"{len(candidates)} h1 candidates extracted",
    )
    ctx.blackboard.toc_result = result
    ctx.blackboard.global_signals["toc_page_count"] = len(toc_pages)
    ctx.blackboard.global_signals["toc_candidate_count"] = len(candidates)
    return ToolResult(
        status="ok",
        payload={
            "toc_pages": sorted(toc_pages),
            "candidate_count": len(candidates),
        },
        latency_ms=int((time.monotonic() - start) * 1000),
    )
