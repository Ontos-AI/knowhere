"""Page anatomy agent for hierarchy-first PDF profiling."""

from typing import TYPE_CHECKING

from app.services.document_agent.manifest import (
    PageAnatomyMap,
    PageFeature,
    PageLabel,
    ShardPlan,
)

if TYPE_CHECKING:
    from app.services.document_agent.profile_agent import ProfileAgent


def __getattr__(name: str):
    if name == "ProfileAgent":
        from app.services.document_agent.profile_agent import ProfileAgent

        return ProfileAgent
    raise AttributeError(name)

__all__ = [
    "PageAnatomyMap",
    "PageFeature",
    "PageLabel",
    "ProfileAgent",
    "ShardPlan",
]
