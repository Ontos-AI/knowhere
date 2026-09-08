"""Result types shared between ``episode.py`` and ``bridge.py``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentStep:
    """One tool call executed during the episode (one LLM turn may yield several)."""

    step_index: int
    tool_name: str
    tool_args: dict[str, Any]
    observation_text: str
    error: str | None
    elapsed_ms: int
    tokens_used_delta: int
    tokens_used_total: int


@dataclass
class EpisodeResult:
    """Everything ``bridge.py`` / the route need after the episode ends."""

    refs: list[dict[str, Any]]
    notes: str
    steps: list[AgentStep] = field(default_factory=list)
    stop_reason: str = "finished"
    tokens_used: int = 0
    model_name: str = ""
