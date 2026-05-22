"""Synchronous ReAct-style coordinator for the document profile agent."""

from __future__ import annotations

import json
import os
from typing import Any

from loguru import logger

from app.services.document_agent.budget import BudgetTracker
from app.services.document_agent.manifest import PageAnatomyMap, ToolContext
from app.services.document_agent.registry import REGISTRY
from app.services.document_agent.state import AgentBlackboard, DocumentAgentState
from app.services.document_agent.tools.persist_anatomy_map import build_anatomy_map
from app.services.document_agent.trace import ParseRunRecorder


TRANSITIONS: dict[str, DocumentAgentState] = {
    "probe.page_features": DocumentAgentState.PROBED,
    "classify.page_kinds": DocumentAgentState.PROBED,
    "collect.boundary_candidates": DocumentAgentState.H1_FOUND,
    "propose.hierarchy_assist": DocumentAgentState.H1_FOUND,
    "propose.shard_plan": DocumentAgentState.H1_FOUND,
    "validate.anatomy_map": DocumentAgentState.VALIDATED,
    "persist.anatomy_map": DocumentAgentState.PERSISTED,
}

REQUIRED_TOOLS = [
    "probe.page_features",
    "classify.page_kinds",
    "collect.boundary_candidates",
    "propose.hierarchy_assist",
    "propose.shard_plan",
    "validate.anatomy_map",
    "persist.anatomy_map",
]


def _parse_tool_calls(response: Any) -> list[dict[str, Any]]:
    choices = getattr(response, "choices", None)
    if not choices:
        return []
    message = choices[0].message
    calls = getattr(message, "tool_calls", None) or []
    parsed: list[dict[str, Any]] = []
    for call in calls:
        function = getattr(call, "function", None)
        if function is None:
            continue
        try:
            args = json.loads(getattr(function, "arguments", "{}") or "{}")
        except json.JSONDecodeError:
            args = {}
        parsed.append(
            {
                "id": getattr(call, "id", ""),
                "name": getattr(function, "name", ""),
                "args": args,
            }
        )
    return parsed


class ProfileCoordinator:
    def __init__(
        self,
        *,
        pdf_path: str,
        job_id: str,
        output_dir: str | None = None,
        db: Any | None = None,
        model: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.state = DocumentAgentState.INIT
        self.blackboard = AgentBlackboard()
        self.budget = BudgetTracker(
            plan_budget=int(os.environ.get("PARSE_AGENT_PLAN_BUDGET", "5000")),
            max_tool_calls=int(os.environ.get("PARSE_AGENT_MAX_TOOL_CALLS", "12")),
        )
        effective_settings = settings or {}
        if model:
            effective_settings["model"] = model
        self.ctx = ToolContext(
            pdf_path=pdf_path,
            job_id=job_id,
            blackboard=self.blackboard,
            budget=self.budget,
            trace=None,
            output_dir=output_dir,
            settings=effective_settings,
        )
        self.trace = ParseRunRecorder(job_id=job_id, db=db)
        self.ctx.trace = self.trace
        self.round_index = 0

    def run(self) -> PageAnatomyMap:
        # The deterministic tool chain is the primary execution contract. LLM is
        # used inside proposal tools where it adds judgement, not to own ordering.
        final_status = "ready"
        try:
            for tool_name in REQUIRED_TOOLS:
                if not self.budget.increment_tool_call():
                    final_status = "fallback"
                    break
                result = REGISTRY.dispatch(tool_name, self.ctx, {}, self.state)
                self.trace.record_step(
                    round_index=self.round_index,
                    actor=f"tool:{tool_name}",
                    action_type="tool_call",
                    result=result,
                    tool_name=tool_name,
                    tool_args={},
                )
                if result.status not in {"ok", "invalid"}:
                    raise RuntimeError(result.error or f"{tool_name} failed")
                self._advance(tool_name)
                self._maybe_advance_composite_state()
                self.round_index += 1
            if self.state == DocumentAgentState.PERSISTED:
                self.state = DocumentAgentState.READY
            anatomy = build_anatomy_map(self.ctx)
            self.trace.flush(
                final_status=final_status,
                summary=anatomy.trace_summary | self.trace.summary(),
            )
            return anatomy
        except Exception as exc:
            logger.error(f"[document_agent] profile failed: {exc}")
            self.state = DocumentAgentState.FAILED
            self.trace.flush(final_status="failed", summary={"error": str(exc)})
            raise

    def _advance(self, tool_name: str) -> None:
        next_state = TRANSITIONS.get(tool_name, self.state)
        self.state = next_state
        self.blackboard.mark(self.state)

    def _maybe_advance_composite_state(self) -> None:
        if (
            self.state == DocumentAgentState.PROBED
            and self.blackboard.page_labels
        ):
            self.state = DocumentAgentState.CLASSIFIED
            self.blackboard.mark(self.state)
        if (
            self.state == DocumentAgentState.H1_FOUND
            and self.blackboard.hierarchy_assist is not None
            and self.blackboard.shard_plan is not None
        ):
            self.state = DocumentAgentState.PLANNED
            self.blackboard.mark(self.state)
