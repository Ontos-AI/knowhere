"""Provider-agnostic ``Harness`` interface for ``agent_explore``.

Any backend-hosted tool-loop implementation (OpenAI-compatible/DeepSeek,
Cursor SDK, ...) implements this single method. ``_run_agent_explore_route``
(``execution/routes.py``) resolves one ``Harness`` via ``AGENT_EXPLORE_HARNESS``
(see ``harness/resolve.py``) and calls it the same way regardless of which
provider is behind it — the route/bridge/budget-object shapes do not change
per harness.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_explore.dispatch import DbFactory
from shared.services.retrieval.agent_explore.types import EpisodeResult


@runtime_checkable
class Harness(Protocol):
    """One backend-hosted tool-loop implementation over ``agent_tools.REGISTRY``."""

    async def run_episode(
        self,
        *,
        db_factory: DbFactory,
        user_id: str,
        namespace: str,
        query: str,
        budget: EpisodeBudget,
    ) -> EpisodeResult:
        """Explore the corpus for ``query`` and return the cited evidence.

        ``db_factory`` is a call-scoped DB session factory (see
        ``dispatch.py``) — implementations must not hold one shared
        ``AsyncSession`` across the whole episode; every ``REGISTRY.dispatch``
        call goes through ``dispatch.dispatch_tool_call(..., db_factory=db_factory)``
        so concurrent tool calls (a real Cursor SDK behavior, not just a
        theoretical one — see ``harness/cursor_harness.py``) are always safe.
        """
        ...
