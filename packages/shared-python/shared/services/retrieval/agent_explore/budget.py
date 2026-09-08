"""Per-episode budget enforcement for ``agent_explore``.

Standalone from ``nav/nav_token_budget.py`` on purpose — this package must
have no runtime dependency on ``nav/`` (see ``config.py``'s module
docstring). Re-reads the same ``RETRIEVAL_NAV_TOKEN_LIMIT`` env var the plan
calls for, so operators keep one token-limit knob across both agentic
routes, but the counting mechanism is a fresh, request-scoped object (this
episode runs as one async function, not a separate thread with recursive
calls), not the nav contextvar machinery ``nav_token_budget`` needed for its
own call shape.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from shared.services.retrieval.agent_explore.config import (
    AGENT_EXPLORE_MAX_STEPS,
    AGENT_EXPLORE_WALL_CLOCK_SECONDS,
)

_ENV_TOKEN_LIMIT = "RETRIEVAL_NAV_TOKEN_LIMIT"
_DEFAULT_TOKEN_LIMIT = 100_000

StopReason = str  # one of: "token_limit" | "max_steps" | "wall_clock"


def resolve_token_limit() -> int:
    """Always a positive limit: env override, else the shared default."""
    try:
        limit = int(os.environ.get(_ENV_TOKEN_LIMIT, "").strip())
    except ValueError:
        limit = 0
    return limit if limit > 0 else _DEFAULT_TOKEN_LIMIT


@dataclass
class EpisodeBudget:
    """Tracks one episode's LLM-token / step / wall-clock spend."""

    token_limit: int = field(default_factory=resolve_token_limit)
    max_steps: int = AGENT_EXPLORE_MAX_STEPS
    wall_clock_seconds: float = AGENT_EXPLORE_WALL_CLOCK_SECONDS
    tokens_used: int = 0
    steps_used: int = 0
    _started_at: float = field(default_factory=time.monotonic, repr=False)

    def record_usage(self, usage: dict[str, Any] | None) -> None:
        try:
            add = int((usage or {}).get("total_tokens", 0) or 0)
        except (TypeError, ValueError):
            add = 0
        if add > 0:
            self.tokens_used += add

    def record_step(self) -> None:
        self.steps_used += 1

    def exhausted(self) -> StopReason | None:
        """Return which budget dimension is exceeded, if any, else None."""
        if self.tokens_used >= self.token_limit:
            return "token_limit"
        if self.steps_used >= self.max_steps:
            return "max_steps"
        if time.monotonic() - self._started_at >= self.wall_clock_seconds:
            return "wall_clock"
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "token_limit": self.token_limit,
            "tokens_used": self.tokens_used,
            "max_steps": self.max_steps,
            "steps_used": self.steps_used,
            "wall_clock_seconds": self.wall_clock_seconds,
            "elapsed_seconds": round(time.monotonic() - self._started_at, 3),
        }
