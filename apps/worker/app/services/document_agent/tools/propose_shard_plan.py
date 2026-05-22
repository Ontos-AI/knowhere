"""Rule-based long-PDF shard planning."""

from __future__ import annotations

import os
import time
from typing import Any

from app.services.document_agent.manifest import Shard, ShardPlan, ToolContext, ToolResult
from app.services.document_agent.registry import register_tool
from app.services.document_agent.state import DocumentAgentState
from app.services.document_agent.validators import single_shard_plan, validate_shard_plan


def _thresholds(ctx: ToolContext) -> tuple[int, int, int]:
    threshold = int(
        ctx.settings.get("shard_threshold")
        or os.environ.get("PARSE_AGENT_SHARD_THRESHOLD", "200")
    )
    min_pages = int(
        ctx.settings.get("min_pages_per_shard")
        or os.environ.get("PARSE_AGENT_MIN_PAGES_PER_SHARD", "20")
    )
    max_pages = int(
        ctx.settings.get("max_pages_per_shard")
        or os.environ.get("PARSE_AGENT_MAX_PAGES_PER_SHARD", "200")
    )
    return threshold, min_pages, max_pages


def _avoid_pages(ctx: ToolContext) -> set[int]:
    return {
        label.page
        for label in ctx.blackboard.page_labels
        if label.kind in {"table_heavy", "landscape"}
    }


def _separator_pages(ctx: ToolContext) -> set[int]:
    return {
        label.page
        for label in ctx.blackboard.page_labels
        if label.kind in {"blank", "separator", "sparse"}
    }


def _nearest_safe_cut(target: int, previous: int, page_count: int, avoid: set[int], separators: set[int]) -> tuple[int, str]:
    window = range(target, max(previous, target - 12), -1)
    for page in window:
        if previous < page < page_count and page in separators and page not in avoid:
            return page, "blank_separator"
    for page in range(target, max(previous, target - 5), -1):
        if previous < page < page_count and page not in avoid:
            return page, "forced_max_size"
    return max(previous + 1, min(target, page_count - 1)), "forced_max_size"


def _cuts_to_shards(cuts: list[tuple[int, str, str, float]], page_count: int) -> list[Shard]:
    shards: list[Shard] = []
    previous = 0
    for cut_page, anchor_type, evidence, confidence in cuts:
        if cut_page <= previous:
            continue
        shards.append(
            Shard(
                shard_index=len(shards),
                page_start=previous + 1,
                page_end=cut_page,
                page_offset=previous,
                anchor_type=anchor_type,  # type: ignore[arg-type]
                anchor_evidence=evidence,
                confidence=confidence,
            )
        )
        previous = cut_page
    if previous < page_count:
        shards.append(
            Shard(
                shard_index=len(shards),
                page_start=previous + 1,
                page_end=page_count,
                page_offset=previous,
                anchor_type="forced_max_size",
                anchor_evidence="final shard",
                confidence=1.0,
            )
        )
    return shards


@register_tool(
    name="propose.shard_plan",
    description="Create a rule-based PDF segment plan for long-document PDF-to-MD execution.",
    allowed_states={DocumentAgentState.H1_FOUND},
)
def propose_shard_plan(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    page_count = ctx.blackboard.page_count
    threshold, min_pages, max_pages = _thresholds(ctx)
    if page_count <= threshold:
        plan = single_shard_plan(page_count)
        ctx.blackboard.shard_plan = plan
        return ToolResult(
            status="ok",
            payload={"enabled": False, "shard_count": len(plan.shards)},
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    avoid = _avoid_pages(ctx)
    separators = _separator_pages(ctx)
    h1_candidates = []
    if ctx.blackboard.hierarchy_assist:
        h1_candidates = ctx.blackboard.hierarchy_assist.prefer_h1_start_pages
    elif ctx.blackboard.h1_result:
        h1_candidates = ctx.blackboard.h1_result.h1_candidates

    cuts: list[tuple[int, str, str, float]] = []
    previous = 0
    for candidate in sorted(h1_candidates, key=lambda item: item.page):
        cut_page = candidate.page - 1
        if cut_page <= previous:
            continue
        if cut_page - previous < min_pages:
            continue
        while cut_page - previous > max_pages:
            forced_target = previous + max_pages
            safe_cut, anchor_type = _nearest_safe_cut(
                forced_target,
                previous,
                page_count,
                avoid,
                separators,
            )
            cuts.append((safe_cut, anchor_type, "max shard size guard", 0.58))
            previous = safe_cut
        if cut_page > previous and cut_page - previous >= min_pages:
            cuts.append(
                (
                    cut_page,
                    "h1_boundary",
                    f"h1 starts at page {candidate.page}: {candidate.title}",
                    candidate.confidence,
                )
            )
            previous = cut_page

    while page_count - previous > max_pages:
        target = previous + max_pages
        safe_cut, anchor_type = _nearest_safe_cut(target, previous, page_count, avoid, separators)
        cuts.append((safe_cut, anchor_type, "tail max shard size guard", 0.58))
        previous = safe_cut

    shards = _cuts_to_shards(cuts, page_count)
    reason = "hierarchy_isolation" if any(cut[1] == "h1_boundary" for cut in cuts) else "too_large"
    plan = ShardPlan(
        enabled=True,
        reason=reason,  # type: ignore[arg-type]
        shards=shards,
        validation=validate_shard_plan(
            ShardPlan(enabled=True, reason=reason, shards=shards),  # type: ignore[arg-type]
            page_count=page_count,
            min_pages=min_pages,
            max_pages=max_pages,
        ),
    )
    ctx.blackboard.shard_plan = plan
    return ToolResult(
        status="ok",
        payload={
            "enabled": plan.enabled,
            "reason": plan.reason,
            "shard_count": len(plan.shards),
            "valid": plan.validation.valid,
        },
        latency_ms=int((time.monotonic() - start) * 1000),
    )
