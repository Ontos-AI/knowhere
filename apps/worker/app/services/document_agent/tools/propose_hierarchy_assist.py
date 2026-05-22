"""Build hierarchy hints from page anatomy signals."""

from __future__ import annotations

import time
from typing import Any

from app.services.document_agent.manifest import (
    BoundaryHint,
    H1Candidate,
    HierarchyAssistPlan,
    PageLabel,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.registry import register_tool
from app.services.document_agent.state import DocumentAgentState
from app.services.document_agent.validators import repair_hierarchy_assist


def _rule_based_plan(labels: list[PageLabel], h1_candidates: list[H1Candidate]) -> HierarchyAssistPlan:
    exclude = [
        label.page
        for label in labels
        if label.kind in {"toc", "blank", "separator", "landscape"}
    ]
    suppress = [
        label.page
        for label in labels
        if label.kind in {"table_heavy", "single_image", "scan_like", "image_heavy"}
    ]
    boundary_hints = [
        BoundaryHint(
            page=candidate.page,
            anchor_type="h1_boundary",
            confidence=candidate.confidence,
            evidence=candidate.evidence | {"title": candidate.title},
        )
        for candidate in h1_candidates
    ]
    scan_like_count = sum(1 for label in labels if label.kind in {"scan_like", "single_image"})
    recommendation = "aggressive" if scan_like_count > max(len(labels) // 3, 0) else "normal"
    if h1_candidates and scan_like_count == 0:
        recommendation = "normal"
    if not h1_candidates and scan_like_count == 0:
        recommendation = "aggressive"
    return HierarchyAssistPlan(
        exclude_pages_from_title_candidates=sorted(set(exclude)),
        prefer_h1_start_pages=sorted(h1_candidates, key=lambda item: item.page),
        suppress_title_pages=sorted(set(suppress)),
        section_boundary_hints=boundary_hints,
        smart_parse_recommendation=recommendation,  # type: ignore[arg-type]
        rationale="Derived from page labels and H1 boundary evidence.",
    )


@register_tool(
    name="propose.hierarchy_assist",
    description="Produce hierarchy hints used by section skeleton extraction and title parsing.",
    allowed_states={DocumentAgentState.H1_FOUND},
)
def propose_hierarchy_assist(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    h1_result = ctx.blackboard.h1_result
    labels = ctx.blackboard.page_labels
    fallback = _rule_based_plan(labels, h1_result.h1_candidates if h1_result else [])
    toc_pages = set(ctx.blackboard.toc_result.toc_pages if ctx.blackboard.toc_result else [])
    plan = fallback

    repaired = repair_hierarchy_assist(
        plan,
        page_count=ctx.blackboard.page_count,
        toc_pages=toc_pages,
    )
    ctx.blackboard.hierarchy_assist = repaired
    return ToolResult(
        status="ok",
        payload={
            "exclude_pages": repaired.exclude_pages_from_title_candidates,
            "suppress_pages": repaired.suppress_title_pages,
            "h1_hint_count": len(repaired.prefer_h1_start_pages),
        },
        latency_ms=int((time.monotonic() - start) * 1000),
        input_summary={
            "page_kind_counts": ctx.blackboard.global_signals.get("page_kind_counts", {}),
            "boundary_candidate_counts": ctx.blackboard.global_signals.get(
                "boundary_candidate_counts", {}
            ),
        },
        output_summary={
            "exclude_pages": repaired.exclude_pages_from_title_candidates[:50],
            "suppress_pages_count": len(repaired.suppress_title_pages),
            "h1_hint_count": len(repaired.prefer_h1_start_pages),
            "smart_parse_recommendation": repaired.smart_parse_recommendation,
            "rationale": repaired.rationale,
        },
    )
