"""In-process tool-calling agentic retrieval route.

See ``.cursor/plans/agentic_corpus_explore_retrieval_c2c4ea21.plan.md``
(Phase 3) for the design. Runs the ``agent_tools`` corpus registry through an
LLM tool-calling loop instead of map-nav's PLANNER/HARVEST/CONTROL episode.
Selected via ``RETRIEVAL_AGENTIC_ROUTER=agent_explore``
(``execution/routes.py``); ``mapnav`` remains the default until this route
passes its Phase 4 evaluation gate.

No runtime dependency on ``shared.services.retrieval.nav`` — see
``config.py``'s module docstring.
"""

from __future__ import annotations

from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_explore.episode import run_agent_explore_episode
from shared.services.retrieval.agent_explore.types import AgentStep, EpisodeResult

__all__ = [
    "AgentStep",
    "EpisodeBudget",
    "EpisodeResult",
    "run_agent_explore_episode",
]
