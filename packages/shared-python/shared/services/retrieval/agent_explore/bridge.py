"""Bridge from an ``agent_explore`` episode to the existing decision-trace shape.

``DecisionTraceStep`` / ``TraceRecorder`` (``shared/services/retrieval/trace/``)
are already provider-agnostic — this module only maps this package's own
``AgentStep`` records onto that shared shape.
"""

from __future__ import annotations

from typing import Any

from shared.services.retrieval.agent_explore.types import AgentStep
from shared.services.retrieval.trace import DecisionTraceStep

# Cap raw trace text before it goes into the public decision_trace response.
TRACE_OBSERVATION_MAX_CHARS = 2_000


def build_decision_trace(steps: list[AgentStep]) -> list[DecisionTraceStep]:
    trace_steps: list[DecisionTraceStep] = []
    for step in steps:
        observation_text = step.observation_text
        if len(observation_text) > TRACE_OBSERVATION_MAX_CHARS:
            observation_text = observation_text[:TRACE_OBSERVATION_MAX_CHARS] + "..."
        phase = "finish" if step.tool_name == "finish" else (
            "stop" if not step.tool_name else "tool_call"
        )
        trace_steps.append(
            DecisionTraceStep(
                step_index=step.step_index,
                agent="agent_explore",
                phase=phase,
                observation={"observation_text": observation_text},
                decision={
                    "action": step.tool_name or "no_tool_call",
                    "args": step.tool_args,
                },
                result={
                    "status": "error" if step.error else "ok",
                    "error": step.error,
                },
                budget={
                    "tokens_used_delta": step.tokens_used_delta,
                    "tokens_used_total": step.tokens_used_total,
                },
                elapsed_ms=step.elapsed_ms,
            )
        )
    return trace_steps


def attach_ref_provenance(
    steps: list[DecisionTraceStep],
    *,
    agent_selected_refs: list[dict[str, Any]] | None,
    fallback_refs: list[dict[str, Any]],
    resolved_refs: list[dict[str, Any]],
    dropped_refs: list[dict[str, Any]],
) -> list[DecisionTraceStep]:
    """Write ref provenance onto the existing finish TRACE step.

    Reuses ``DecisionTraceStep.result``. If the episode never called finish,
    append one finish step so the same public TRACE shape still holds the
    four lists.
    """
    provenance = {
        "agent_selected_refs": agent_selected_refs,
        "fallback_refs": fallback_refs,
        "resolved_refs": resolved_refs,
        "dropped_refs": dropped_refs,
    }
    for step in reversed(steps):
        if step.phase == "finish":
            step.result = {**step.result, **provenance}
            return steps
    steps.append(
        DecisionTraceStep(
            step_index=len(steps),
            agent="agent_explore",
            phase="finish",
            observation={"observation_text": "finish was not called"},
            decision={"action": "finish", "args": {"refs": None}},
            result={"status": "ok", "error": None, **provenance},
        )
    )
    return steps
