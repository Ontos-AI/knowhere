"""Recursive-dispatch map navigation for RealData experiments.

LEGACY, PENDING REPLACEMENT: this package implements the map-nav
PLANNER/HARVEST/CONTROL episode
(``run_nav_episode``), the current default agentic retrieval route via
``execution.routes._run_mapnav_route``. It will be superseded by
``agent_explore`` after its evaluation gate, then trimmed in
Phase 5 to whatever this package still owns and nothing else uses (the BM25
scorer in ``knowhere_hybrid.py``, the ``NodeFilter`` predicate in
``nav_node_filter.py``, and snapshot loading are already planned to be
reused by the new ``agent_tools/``, not deleted). Do not add new PLANNER /
HARVEST / CONTROL capabilities here; new agentic-retrieval work belongs in
``shared/services/retrieval/agent_tools/`` and
``shared/services/retrieval/agent_explore/``.
"""

from .nav_types import (
    ActionKind,
    NavConfig,
    NavState,
    SubgoalResult,
)
from .nav_agent import run_nav_episode
from .nav_plan import (
    Contract,
    RetrievalPlan,
    Subgoal,
    bind_slots,
    plan_query,
)
from .nav_orchestrate import execute_plan

__all__ = [
    "ActionKind",
    "NavConfig",
    "NavState",
    "SubgoalResult",
    "run_nav_episode",
    "Contract",
    "RetrievalPlan",
    "Subgoal",
    "bind_slots",
    "plan_query",
    "execute_plan",
]
