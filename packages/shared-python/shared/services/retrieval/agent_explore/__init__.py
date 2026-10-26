"""In-process tool-calling agentic retrieval route.

Agentic path when ``use_agentic`` is true, or when it is unset and the query
router selects agent. Runs the ``agent_tools`` corpus registry through an LLM
tool-calling loop.

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
