"""Result types shared between ``episode.py`` and ``bridge.py``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shared.services.retrieval.agent_explore.evidence_pool import Candidate


@dataclass
class AgentStep:
    """One tool call executed during the episode (one LLM turn may yield several)."""

    step_index: int
    tool_name: str
    tool_args: dict[str, Any]
    observation_text: str
    error: str | None
    elapsed_ms: int
    round_index: int
    # corpus.read only: one {ref, status, reason?} entry per requested ref,
    # kept whole in the trace (observation_text is capped there).
    ref_status: list[dict[str, Any]] | None = None
    candidates: list[str] | None = None
    # corpus.pick only.
    picked: list[str] | None = None
    pick_rejected: list[dict[str, str]] | None = None


@dataclass
class EpisodeResult:
    """Everything ``bridge.py`` / the route need after the episode ends."""

    pool: list[Candidate]
    notes: str
    steps: list[AgentStep] = field(default_factory=list)
    stop_reason: str = "finished"
    tokens_used: int = 0
    model_name: str = ""
