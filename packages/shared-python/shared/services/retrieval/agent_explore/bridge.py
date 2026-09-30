"""Bridge from an ``agent_explore`` episode to the existing decision-trace shape.

``DecisionTraceStep`` / ``TraceRecorder`` (``shared/services/retrieval/trace/``)
are already provider-agnostic — this module only maps this package's own
``AgentStep`` records onto that shared shape.
"""

from __future__ import annotations

from shared.services.retrieval.agent_explore.evidence_pool import (
    Candidate,
    pool_trace_records,
)
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
        observation: dict[str, object] = {"observation_text": observation_text}
        if step.ref_status is not None:
            observation["ref_status"] = step.ref_status
        if step.candidates is not None:
            observation["candidates"] = step.candidates
        decision: dict[str, object] = {
            "action": step.tool_name or "no_tool_call",
            "args": step.tool_args,
            "round_index": step.round_index,
        }
        result: dict[str, object] = {
            "status": "error" if step.error else "ok",
            "error": step.error,
        }
        if step.picked is not None:
            result["picked"] = step.picked
        if step.pick_rejected is not None:
            result["pick_rejected"] = step.pick_rejected
        trace_steps.append(
            DecisionTraceStep(
                step_index=step.step_index,
                agent="agent_explore",
                phase=phase,
                observation=observation,
                decision=decision,
                result=result,
                budget={},
                elapsed_ms=step.elapsed_ms,
            )
        )
    return trace_steps


def attach_evidence_pool(
    steps: list[DecisionTraceStep],
    pool: list[Candidate],
) -> list[DecisionTraceStep]:
    """Write the evidence pool onto the existing finish TRACE step.

    If the episode never called finish, append one finish step so the same
    public TRACE shape still holds the pool.
    """
    records = pool_trace_records(pool)
    for step in reversed(steps):
        if step.phase == "finish":
            step.result = {**step.result, "evidence_pool": records}
            return steps
    steps.append(
        DecisionTraceStep(
            step_index=len(steps),
            agent="agent_explore",
            phase="finish",
            observation={"observation_text": "finish was not called"},
            decision={"action": "finish", "args": {}},
            result={"status": "ok", "error": None, "evidence_pool": records},
        )
    )
    return steps
