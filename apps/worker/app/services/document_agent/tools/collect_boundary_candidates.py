"""Collect candidate split pages without deciding where to split."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from app.services.document_agent.manifest import (
    BoundaryCandidate,
    H1BoundaryResult,
    TocResult,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.registry import register_tool
from app.services.document_agent.state import DocumentAgentState


BOUNDARY_PAGE_KINDS = {"blank", "sparse", "toc"}
PRIORITY_BY_KIND = {
    "h1": 100,
    "toc": 80,
    "blank": 45,
    "sparse": 40,
}


def _feature_by_page(ctx: ToolContext) -> dict[int, Any]:
    return {feature.page: feature for feature in ctx.blackboard.page_features}


def _candidate_evidence(ctx: ToolContext, page: int, kind: str) -> dict[str, Any]:
    feature = _feature_by_page(ctx).get(page)
    if feature is None:
        return {}
    return {
        "source": "page_label",
        "label_kind": kind,
        "position_ratio": round(page / max(ctx.blackboard.page_count, 1), 4),
        "raw_text_length": feature.raw_text_length,
        "image_coverage": feature.image_coverage,
        "table_count": feature.table_count,
        "drawings_count": feature.drawings_count,
        "text_preview": feature.text_lines_preview[:5],
    }


@register_tool(
    name="collect.boundary_candidates",
    description="Collect sparse, blank, TOC, and H1 pages as split candidates without making split decisions.",
    allowed_states={DocumentAgentState.H1_FOUND},
)
def collect_boundary_candidates(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    candidates: list[BoundaryCandidate] = []
    seen: set[tuple[int, str]] = set()

    # Upstream tools (extract.toc_with_boundaries, match.h1_pages) populate
    # these before collect runs. Provide safe defaults if they were skipped.
    if ctx.blackboard.toc_result is None:
        ctx.blackboard.toc_result = TocResult(method="none")
    if ctx.blackboard.h1_result is None:
        ctx.blackboard.h1_result = H1BoundaryResult(method="none")

    for label in ctx.blackboard.page_labels:
        if label.kind not in BOUNDARY_PAGE_KINDS:
            continue
        key = (label.page, label.kind)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            BoundaryCandidate(
                page=label.page,
                kind=label.kind,  # type: ignore[arg-type]
                priority=PRIORITY_BY_KIND.get(label.kind, 0),
                confidence=label.confidence,
                evidence=_candidate_evidence(ctx, label.page, label.kind),
            )
        )

    # Add H1 candidates from upstream match.h1_pages as highest-priority
    # boundary hints for shard planning.
    if ctx.blackboard.h1_result:
        for h1 in ctx.blackboard.h1_result.h1_candidates:
            key = (h1.page, "h1")
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                BoundaryCandidate(
                    page=h1.page,
                    kind="h1",
                    priority=PRIORITY_BY_KIND["h1"],
                    confidence=h1.confidence,
                    evidence={
                        "source": h1.source,
                        "title": h1.title,
                        "matched_line": h1.matched_line,
                        "position_ratio": round(
                            h1.page / max(ctx.blackboard.page_count, 1), 4
                        ),
                        **h1.evidence,
                    },
                )
            )

    candidates.sort(key=lambda item: (item.page, -item.priority))
    ctx.blackboard.boundary_candidates = candidates
    counts = Counter(candidate.kind for candidate in candidates)
    ctx.blackboard.global_signals["boundary_candidate_counts"] = dict(counts)

    return ToolResult(
        status="ok",
        payload={
            "candidate_count": len(candidates),
            "candidate_counts": dict(counts),
        },
        latency_ms=int((time.monotonic() - start) * 1000),
        input_summary={
            "page_count": ctx.blackboard.page_count,
            "page_kind_counts": ctx.blackboard.global_signals.get("page_kind_counts", {}),
        },
        output_summary={
            "candidate_counts": dict(counts),
            "sample_candidates": [candidate.to_dict() for candidate in candidates[:20]],
        },
    )
