"""Validate the current anatomy blackboard."""

from __future__ import annotations

import os
import time
from typing import Any

from app.services.document_agent.manifest import PageAnatomyMap, ToolContext, ToolResult
from app.services.document_agent.registry import register_tool
from app.services.document_agent.state import DocumentAgentState
from app.services.document_agent.validators import validate_anatomy_map


def _thresholds(ctx: ToolContext) -> tuple[int, int]:
    min_pages = int(
        ctx.settings.get("min_pages_per_shard")
        or os.environ.get("PARSE_AGENT_MIN_PAGES_PER_SHARD", "20")
    )
    max_pages = int(
        ctx.settings.get("max_pages_per_shard")
        or os.environ.get("PARSE_AGENT_MAX_PAGES_PER_SHARD", "200")
    )
    return min_pages, max_pages


@register_tool(
    name="validate.anatomy_map",
    description="Validate page anatomy, hierarchy hints, and shard coverage.",
    allowed_states={DocumentAgentState.PLANNED},
)
def validate_current_anatomy(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    if not (
        ctx.blackboard.toc_result
        and ctx.blackboard.h1_result
        and ctx.blackboard.hierarchy_assist
        and ctx.blackboard.shard_plan
    ):
        return ToolResult(
            status="error",
            error="blackboard is missing anatomy outputs",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    anatomy = PageAnatomyMap(
        job_id=ctx.job_id,
        file_path=ctx.pdf_path,
        page_count=ctx.blackboard.page_count,
        page_features=ctx.blackboard.page_features,
        page_labels=ctx.blackboard.page_labels,
        toc_result=ctx.blackboard.toc_result,
        h1_result=ctx.blackboard.h1_result,
        hierarchy_assist=ctx.blackboard.hierarchy_assist,
        shard_plan=ctx.blackboard.shard_plan,
        global_signals=ctx.blackboard.global_signals,
        trace_summary={},
    )
    min_pages, max_pages = _thresholds(ctx)
    report = validate_anatomy_map(anatomy, min_pages=min_pages, max_pages=max_pages)
    ctx.blackboard.validation_report = report.to_dict()
    if ctx.blackboard.shard_plan:
        ctx.blackboard.shard_plan.validation = report if not report.valid else ctx.blackboard.shard_plan.validation
    return ToolResult(
        status="ok" if report.valid else "invalid",
        payload=report.to_dict(),
        latency_ms=int((time.monotonic() - start) * 1000),
    )
