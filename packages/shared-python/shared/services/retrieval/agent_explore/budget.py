"""Per-episode budget enforcement for ``agent_explore``.

Standalone from ``nav/nav_token_budget.py`` on purpose — this package must
have no runtime dependency on ``nav/`` (see ``config.py``'s module
docstring). Exploration is limited by steps; the wall clock is a
service-level guard. Tokens are not limited: ``tokens_used`` only records the
episode total.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from shared.services.retrieval.agent_explore.config import (
    AGENT_EXPLORE_MAX_STEPS,
    AGENT_EXPLORE_WALL_CLOCK_SECONDS,
)

StopReason = str  # one of: "max_steps" | "wall_clock"


@dataclass
class EpisodeBudget:
    """Tracks one episode's step / wall-clock spend and its token total."""

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

    def remaining_seconds(self) -> float:
        return self.wall_clock_seconds - (time.monotonic() - self._started_at)

    def exhausted(self) -> StopReason | None:
        """Return which budget dimension is exceeded, if any, else None."""
        if self.steps_used >= self.max_steps:
            return "max_steps"
        if self.remaining_seconds() <= 0:
            return "wall_clock"
        return None
