"""Bridge from an ``agent_explore`` episode to the existing decision-trace shape.

``DecisionTraceStep`` / ``TraceRecorder`` (``shared/services/retrieval/trace/``)
are already provider-agnostic — this module only maps this package's own
``AgentStep`` records onto that shared shape, mirroring what
``trace/mapnav.py`` does for the map-nav episode object, without importing
anything from ``nav/``.
"""

from __future__ import annotations

from shared.services.retrieval.agent_explore.types import AgentStep
from shared.services.retrieval.trace import DecisionTraceStep

# Mirrors nav_config.MAPNAV_TRACE_RAW_CHARS's existing practice of capping
# raw trace text before it goes into the public decision_trace response —
# redeclared locally (not imported) to keep this package decoupled from
# nav_config.py per config.py's module docstring.
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
