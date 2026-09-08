"""In-process tool-calling agentic retrieval route.

Default agentic path when ``use_agentic`` is unset or true. Runs the
``agent_tools`` corpus registry through an LLM tool-calling loop. Map-nav is
archived under ``deprecated/mapnav/`` and is not a live route.

Which provider runs that loop (Cursor SDK or OpenAI-compatible) is the
``AGENT_EXPLORE_HARNESS`` switch resolved via ``resolve_harness()``.
"""

from __future__ import annotations

from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_explore.harness import Harness, resolve_harness
from shared.services.retrieval.agent_explore.types import AgentStep, EpisodeResult

__all__ = [
    "AgentStep",
    "EpisodeBudget",
    "EpisodeResult",
    "Harness",
    "resolve_harness",
]
