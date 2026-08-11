"""Decision-trace types for retrieval (moved out of agentic/core)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class DecisionTraceStep:
    """Uniform observe-act-result trace entry exposed to downstream agents."""

    step_index: int
    agent: str
    phase: str
    observation: dict[str, Any]
    decision: dict[str, Any]
    result: dict[str, Any]
    parent_step_index: int | None = None
    document_id: str | None = None
    document: str | None = None
    scope: str | None = None
    budget: dict[str, Any] | None = None
    elapsed_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "step_index": self.step_index,
            "agent": self.agent,
            "parent_step_index": self.parent_step_index,
            "phase": self.phase,
            "document_id": self.document_id,
            "document": self.document,
            "scope": self.scope,
            "observation": self.observation,
            "decision": self.decision,
            "result": self.result,
        }
        if self.budget is not None:
            data["budget"] = self.budget
        if self.elapsed_ms is not None:
            data["elapsed_ms"] = self.elapsed_ms
        return data
