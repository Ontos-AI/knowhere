"""Persist anatomy map artifacts and optional database records."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.services.document_agent.manifest import PageAnatomyMap, ToolContext, ToolResult

DOC_PROFILE_FILENAME = "doc_profile.json"


def _artifact_dir(ctx: ToolContext) -> Path:
    if ctx.output_dir:
        return Path(ctx.output_dir)
    import os

    base = Path(os.path.expanduser("~/.knowhere/_debug_profile"))
    return base / Path(ctx.pdf_path).stem


def resolve_doc_profile_path(agent_or_package_dir: Path) -> Path:
    """Return the package-root path for the production doc profile artifact.

    ProfileCoordinator writes under ``_doc_agent/``; the client-facing file lives
    one level up at the result package root. When the coordinator output dir is
    already the package root (tests / standalone profiling), write in-place.
    """
    if agent_or_package_dir.name == "_doc_agent":
        return agent_or_package_dir.parent / DOC_PROFILE_FILENAME
    return agent_or_package_dir / DOC_PROFILE_FILENAME


def build_anatomy_map(ctx: ToolContext) -> PageAnatomyMap:
    if not (
        ctx.blackboard.toc_result
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
        toc_page_offset=ctx.blackboard.toc_page_offset,
        global_signals=ctx.blackboard.global_signals,
        trace_summary={
            "budget": ctx.budget.snapshot(),
            "validation": ctx.blackboard.validation_report,
        },
    )


def persist_anatomy_map(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    anatomy = build_anatomy_map(ctx)
    agent_dir = _artifact_dir(ctx)
    agent_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = resolve_doc_profile_path(agent_dir)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(anatomy.to_dict(), ensure_ascii=False, indent=2)
    artifact_path.write_text(payload, encoding="utf-8")
    if ctx.trace:
        ctx.trace.set_anatomy_map(anatomy, str(artifact_path))
    return ToolResult(
        status="ok",
        payload={"artifact_path": str(artifact_path)},
        latency_ms=int((time.monotonic() - start) * 1000),
    )
