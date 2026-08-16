"""ReAct-style executor for the document profile agent."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from app.services.document_agent.manifest import (
    AgentVerdict,
    ReflexionDecision,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.executor.prompts import REFLEXION_INSTRUCTIONS
from app.services.document_agent.registry import ToolRegistry
from shared.utils.token_estimate import estimate_tokens


@dataclass
class ExecutorResult:
    verdict: AgentVerdict
    rounds: int


def _compact_blackboard(ctx: ToolContext) -> dict[str, Any]:
    return {
        "page_count": ctx.blackboard.page_count,
        "page_kind_counts": ctx.blackboard.global_signals.get("page_kind_counts", {}),
        "doc_stats": ctx.blackboard.doc_stats,
        "extrema_pages": ctx.blackboard.extrema_pages,
        "document_profile": ctx.blackboard.document_profile.to_dict()
        if ctx.blackboard.document_profile
        else None,
        "toc_anchor_pages": [anchor.page for anchor in ctx.blackboard.toc_anchor_pages],
        "toc_pages": (
            ctx.blackboard.toc_result.toc_pages if ctx.blackboard.toc_result else []
        ),
        "toc_hierarchies_count": len(ctx.blackboard.toc_hierarchies or []),
        "shard_plan": ctx.blackboard.shard_plan.to_dict()
        if ctx.blackboard.shard_plan
        else None,
        "validation_report": ctx.blackboard.validation_report,
        "verdict": ctx.blackboard.verdict.to_dict()
        if ctx.blackboard.verdict
        else None,
        "grep_history": ctx.blackboard.global_signals.get("grep_history", [])[-3:],
        "budget": ctx.budget.snapshot(),
    }


def _coerce_legacy_finish(data: dict[str, Any]) -> ReflexionDecision:
    """Map obsolete ``action=verdict_now`` into a real tool call.

    Finish belongs exclusively to the ``verdict`` tool. A bare ``verdict_now``
    without an explicit status is treated as ready-to-shard, never as abort.
    """
    rationale = str(data.get("rationale") or "")
    raw_verdict = data.get("verdict")
    if isinstance(raw_verdict, dict):
        status = str(raw_verdict.get("status") or "").strip().lower()
        if status in {"success", "abort"}:
            return ReflexionDecision(
                action="tool_call",
                rationale=rationale,
                tool_name="verdict",
                tool_args={
                    "status": status,
                    "rationale": str(
                        raw_verdict.get("rationale") or rationale or status
                    ),
                },
            )
    return ReflexionDecision(
        action="tool_call",
        rationale=rationale or "Legacy verdict_now without status; propose shard plan.",
        tool_name="propose.shard_plan",
        tool_args={},
    )


def _parse_decision(raw: str) -> ReflexionDecision:
    data = json.loads(raw)
    action = str(data.get("action") or "tool_call").strip().lower()
    if action == "verdict_now":
        return _coerce_legacy_finish(data if isinstance(data, dict) else {})
    if action != "tool_call":
        action = "tool_call"
    return ReflexionDecision(
        action="tool_call",
        rationale=str(data.get("rationale") or ""),
        tool_name=data.get("tool_name"),
        tool_args=dict(data.get("tool_args") or {}),
        verdict=None,
    )


class ReActExecutor:
    def __init__(
        self,
        ctx: ToolContext,
        *,
        registry: ToolRegistry,
        max_rounds: int = 30,
        initial_decision: ReflexionDecision | None = None,
    ) -> None:
        self.ctx = ctx
        self.registry = registry
        self.max_rounds = max_rounds
        self._initial_decision = initial_decision

    def run(self) -> ExecutorResult:
        for round_index in range(self.max_rounds):
            pending_recovery_verdict: AgentVerdict | None = None
            decision, result = self._next_decision(round_index)
            if decision.action != "tool_call":
                # Defensive: only tool_call is a legal executor step.
                decision = ReflexionDecision(
                    action="tool_call",
                    rationale=decision.rationale
                    or "Non-tool executor action coerced to propose.shard_plan.",
                    tool_name="propose.shard_plan",
                    tool_args={},
                )
            self.ctx.blackboard.global_signals.setdefault("reflexion_decisions", []).append(
                decision.to_dict()
            )
            if self.ctx.trace:
                self.ctx.trace.record_step(
                    round_index=round_index,
                    actor=f"executor:r{round_index}",
                    action_type="reflexion",
                    result=result,
                    tool_name=decision.tool_name,
                    tool_args=decision.tool_args,
                )

            tool_name, tool_args = self._resolve_tool_call(decision)
            if tool_name == "verdict" and str(tool_args.get("status") or "") == "success":
                if not (
                    self.ctx.blackboard.validation_report
                    and self.ctx.blackboard.validation_report.get("valid") is True
                ):
                    decision = ReflexionDecision(
                        action="tool_call",
                        rationale=(
                            "Validate the anatomy map before accepting a success verdict."
                        ),
                        tool_name="validate.anatomy_map",
                        tool_args={},
                    )
                    tool_name, tool_args = self._resolve_tool_call(decision)

            if not tool_name:
                verdict = AgentVerdict(
                    status="abort",
                    rationale="Executor did not choose a tool.",
                )
                self.ctx.blackboard.verdict = verdict
                return ExecutorResult(verdict=verdict, rounds=round_index + 1)

            tool_result = self.registry.dispatch(tool_name, self.ctx, tool_args)
            if self.ctx.trace:
                self.ctx.trace.record_step(
                    round_index=round_index,
                    actor=f"tool:{tool_name}",
                    action_type="tool_call",
                    result=tool_result,
                    tool_name=tool_name,
                    tool_args=tool_args,
                )
            self.ctx.blackboard.step_history.append(
                {
                    "round": round_index,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "status": tool_result.status,
                    "error": tool_result.error,
                }
            )
            if tool_result.status == "error":
                pending_recovery_verdict = AgentVerdict(
                    status="abort",
                    rationale=tool_result.error or f"{tool_name} failed",
                )
            elif tool_result.status == "precondition_unmet":
                pending_recovery_verdict = AgentVerdict(
                    status="abort",
                    rationale=tool_result.error or f"{tool_name} precondition unmet",
                )

            if self.ctx.blackboard.verdict is not None:
                return ExecutorResult(
                    verdict=self.ctx.blackboard.verdict,
                    rounds=round_index + 1,
                )
            if pending_recovery_verdict is not None and self._is_deterministic_mode():
                self.ctx.blackboard.verdict = pending_recovery_verdict
                return ExecutorResult(
                    verdict=pending_recovery_verdict,
                    rounds=round_index + 1,
                )

        verdict = AgentVerdict(status="abort", rationale="Maximum executor rounds reached.")
        self.ctx.blackboard.verdict = verdict
        return ExecutorResult(verdict=verdict, rounds=self.max_rounds)

    def _resolve_tool_call(
        self,
        decision: ReflexionDecision,
    ) -> tuple[str | None, dict[str, Any]]:
        if decision.action == "tool_call" and decision.tool_name:
            return decision.tool_name, decision.tool_args
        return None, {}

    def _is_deterministic_mode(self) -> bool:
        return not (self.ctx.settings.get("executor_model") or self.ctx.settings.get("model"))

    def _next_decision(self, round_index: int) -> tuple[ReflexionDecision, ToolResult]:
        if round_index == 0 and self._initial_decision is not None:
            decision = self._initial_decision
            if decision.action != "tool_call":
                decision = ReflexionDecision(
                    action="tool_call",
                    rationale=decision.rationale
                    or "Initial non-tool decision coerced to propose.shard_plan.",
                    tool_name="propose.shard_plan",
                    tool_args={},
                )
            elif not decision.tool_name:
                decision = ReflexionDecision(
                    action="tool_call",
                    rationale=decision.rationale
                    or "Initial decision missing tool; propose shard plan.",
                    tool_name="propose.shard_plan",
                    tool_args={},
                )
            return decision, ToolResult(status="ok", payload=decision.to_dict())
        model = self.ctx.settings.get("executor_model") or self.ctx.settings.get("model")
        if not model:
            decision = self._deterministic_decision()
            return decision, ToolResult(status="ok", payload=decision.to_dict())

        payload = {
            "blackboard": _compact_blackboard(self.ctx),
            "history_tail": self.ctx.blackboard.step_history[-6:],
            "available_tools": self.registry.openai_specs(self.ctx.blackboard),
            "round_index": round_index,
        }
        prompt = REFLEXION_INSTRUCTIONS + "\nPayload:\n" + json.dumps(
            payload,
            ensure_ascii=False,
        )
        est = estimate_tokens(prompt)
        if not self.ctx.budget.try_reserve("plan", est):
            decision = ReflexionDecision(
                action="tool_call",
                rationale="Planner budget exhausted.",
                tool_name="verdict",
                tool_args={
                    "status": "abort",
                    "rationale": "Planner budget exhausted.",
                },
            )
            return decision, ToolResult(
                status="ok",
                payload=decision.to_dict(),
                input_summary=payload,
            )
        start = time.monotonic()
        try:
            from shared.services.ai.llm_overrides import get_text_client

            client, model = get_text_client(requested_model=model)
            raw, usage = client.chat_completion_with_usage(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.0,
                max_tokens=1200,
                response_format={"type": "json_object"},
                usage_task="document_agent.react_loop",
            )
            self.ctx.budget.commit(
                "plan",
                actual=usage.get("total_tokens", est),
                est=est,
            )
            decision = _parse_decision(raw)
            return decision, ToolResult(
                status="ok",
                payload=decision.to_dict(),
                latency_ms=int((time.monotonic() - start) * 1000),
                tokens_used=usage.get("total_tokens", 0),
                input_summary=payload,
                debug={"prompt_text": prompt, "raw_response": raw},
            )
        except Exception:
            self.ctx.budget.refund("plan", est=est)
            raise

    def _deterministic_decision(self) -> ReflexionDecision:
        if self.ctx.blackboard.shard_plan is None:
            return ReflexionDecision(
                action="tool_call",
                rationale="Create a shard plan.",
                tool_name="propose.shard_plan",
                tool_args={},
            )
        if not self.ctx.blackboard.validation_report:
            return ReflexionDecision(
                action="tool_call",
                rationale="Validate the current shard plan.",
                tool_name="validate.anatomy_map",
                tool_args={},
            )
        if self.ctx.blackboard.validation_report.get("valid") is True:
            return ReflexionDecision(
                action="tool_call",
                rationale="Validation succeeded; finish profile run.",
                tool_name="verdict",
                tool_args={
                    "status": "success",
                    "rationale": "Validation succeeded; finishing profile run.",
                },
            )
        # Validation failed: fallback to single shard instead of aborting.
        # Clear the invalid plan and re-propose as a single shard.
        from app.services.document_agent.tools.propose_shard_plan import single_shard_plan

        self.ctx.blackboard.shard_plan = single_shard_plan(
            self.ctx.blackboard.page_count
        )
        self.ctx.blackboard.validation_report = None
        return ReflexionDecision(
            action="tool_call",
            rationale="Validation failed; falling back to single shard plan.",
            tool_name="validate.anatomy_map",
            tool_args={},
        )
