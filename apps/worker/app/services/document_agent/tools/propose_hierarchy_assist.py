"""Build hierarchy hints from page anatomy signals."""

from __future__ import annotations

import json
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
from shared.utils.token_estimate import estimate_tokens


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


def _parse_llm_plan(raw: str, fallback: HierarchyAssistPlan) -> HierarchyAssistPlan:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return fallback
    if not isinstance(data, dict):
        return fallback
    plan = fallback
    plan.exclude_pages_from_title_candidates = [
        int(page) for page in data.get("exclude_pages_from_title_candidates", plan.exclude_pages_from_title_candidates)
    ]
    plan.suppress_title_pages = [
        int(page) for page in data.get("suppress_title_pages", plan.suppress_title_pages)
    ]
    recommendation = data.get("smart_parse_recommendation")
    if recommendation in {"off", "normal", "aggressive"}:
        plan.smart_parse_recommendation = recommendation
    if isinstance(data.get("rationale"), str):
        plan.rationale = data["rationale"]
    return plan


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

    model = ctx.settings.get("model")
    if model:
        payload = {
            "page_count": ctx.blackboard.page_count,
            "page_kind_counts": ctx.blackboard.global_signals.get("page_kind_counts", {}),
            "toc_pages": sorted(toc_pages),
            "h1_candidates": [
                candidate.to_dict() for candidate in (h1_result.h1_candidates if h1_result else [])
            ],
            "candidate_exclude_pages": fallback.exclude_pages_from_title_candidates,
            "candidate_suppress_pages": fallback.suppress_title_pages,
        }
        prompt = (
            "Return strict JSON for hierarchy assistance. "
            "Use only page numbers present in the payload. "
            "Do not invent headings. "
            "Fields: exclude_pages_from_title_candidates, suppress_title_pages, "
            "smart_parse_recommendation, rationale.\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        est = estimate_tokens(prompt)
        if ctx.budget.try_reserve("plan", est):
            try:
                from shared.services.ai.openai_compatible_client_sync import get_openai_client

                client = get_openai_client(model=model)
                raw, usage = client.chat_completion_with_usage(
                    messages=prompt,
                    model=model,
                    temperature=0.0,
                    max_tokens=1200,
                    response_format={"type": "json_object"},
                )
                ctx.budget.commit("plan", actual=usage.get("total_tokens", est), est=est)
                plan = _parse_llm_plan(raw, fallback)
            except Exception:
                ctx.budget.refund("plan", est=est)
                plan = fallback
        else:
            plan = fallback
    else:
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
    )
