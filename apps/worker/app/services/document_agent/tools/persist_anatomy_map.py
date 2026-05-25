"""Persist anatomy map artifacts and optional database records."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from app.services.document_agent.manifest import PageAnatomyMap, ToolContext, ToolResult


def _artifact_dir(ctx: ToolContext) -> Path:
    if ctx.output_dir:
        return Path(ctx.output_dir)
    base = Path(os.path.expanduser("~/.knowhere/_debug_profile"))
    return base / Path(ctx.pdf_path).stem


def build_anatomy_map(ctx: ToolContext) -> PageAnatomyMap:
    if not (
        ctx.blackboard.toc_result
        and ctx.blackboard.h1_result
        and ctx.blackboard.shard_plan
    ):
        raise ValueError("cannot build anatomy map from incomplete blackboard")
    return PageAnatomyMap(
        job_id=ctx.job_id,
        file_path=ctx.pdf_path,
        page_count=ctx.blackboard.page_count,
        page_features=ctx.blackboard.page_features,
        page_labels=ctx.blackboard.page_labels,
        toc_result=ctx.blackboard.toc_result,
        h1_result=ctx.blackboard.h1_result,
        shard_plan=ctx.blackboard.shard_plan,
        document_profile=ctx.blackboard.document_profile,
        toc_hierarchies=ctx.blackboard.toc_hierarchies,
        global_signals=ctx.blackboard.global_signals,
        trace_summary={
            "budget": ctx.budget.snapshot(),
            "validation": ctx.blackboard.validation_report,
        },
    )


def persist_anatomy_map(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    anatomy = build_anatomy_map(ctx)
    output_dir = _artifact_dir(ctx)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = output_dir / "anatomy_map.json"
    artifact_path.write_text(
        json.dumps(anatomy.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if ctx.trace:
        ctx.trace.set_anatomy_map(anatomy, str(artifact_path))
    return ToolResult(
        status="ok",
        payload={"artifact_path": str(artifact_path)},
        latency_ms=int((time.monotonic() - start) * 1000),
    )
