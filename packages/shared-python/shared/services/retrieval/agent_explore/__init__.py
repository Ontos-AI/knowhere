"""In-process tool-calling agentic retrieval route.

See ``.cursor/plans/agentic_corpus_explore_retrieval_c2c4ea21.plan.md``
(Phase 3) for the design. Runs the ``agent_tools`` corpus registry through an
LLM tool-calling loop instead of map-nav's PLANNER/HARVEST/CONTROL episode.
Selected via ``RETRIEVAL_AGENTIC_ROUTER=agent_explore``
(``execution/routes.py``); ``mapnav`` remains the default until this route
passes its Phase 4 evaluation gate.

Which provider runs that loop (OpenAI-compatible/DeepSeek, Cursor SDK) is a
second, independent switch — ``AGENT_EXPLORE_HARNESS`` — resolved via
``resolve_harness()``. See ``harness/`` (Phase 3.5) for the pluggable
``Harness`` interface and its two implementations.

No runtime dependency on ``shared.services.retrieval.nav`` — see
``config.py``'s module docstring.
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
