"""Agent verdict tool."""

from __future__ import annotations

import time
from typing import Any

from app.services.document_agent.manifest import AgentVerdict, ToolContext, ToolResult
from app.services.document_agent.registry import has_shard_plan, register_tool


@register_tool(
    name="verdict",
    description="Finish the document profile run with success or abort.",
    parameters={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["success", "abort"]},
            "rationale": {"type": "string"},
        },
        "required": ["status", "rationale"],
    },
    preconditions=(has_shard_plan,),
)
def verdict(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    status = str(args.get("status") or "abort")
    if status not in {"success", "abort"}:
        status = "abort"
    ctx.blackboard.verdict = AgentVerdict(
        status=status,  # type: ignore[arg-type]
        rationale=str(args.get("rationale") or ""),
    )
    return ToolResult(
        status="ok",
        payload=ctx.blackboard.verdict.to_dict(),
        latency_ms=int((time.monotonic() - start) * 1000),
    )

