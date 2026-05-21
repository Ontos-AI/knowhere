"""Document Anatomy Agent package.

Phase 0 (Anatomy): ``DocumentAnatomyAgent`` produces a ``PageMap`` before
any PDF parsing begins, enabling semantically-correct shard decisions.

Phase 1 (Shard planning): ``ShardManifest`` / ``ShardSignal`` / … are the
original shard-planning primitives retained for backward compatibility.
"""

from app.services.document_agent.agent import DocumentAnatomyAgent
from app.services.document_agent.manifest import (
    GlobalSignals,
    ShardManifest,
    ShardSignal,
    SpecialPage,
)
from app.services.document_agent.page_map import (
    CutPoint,
    H1BoundaryResult,
    H1Match,
    PageFeature,
    PageMap,
    Shard,
)

__all__ = [
    # Phase 0 — Anatomy Agent
    "DocumentAnatomyAgent",
    "CutPoint",
    "H1BoundaryResult",
    "H1Match",
    "PageFeature",
    "PageMap",
    "Shard",
    # Phase 1 — Shard planning primitives
    "GlobalSignals",
    "ShardManifest",
    "ShardSignal",
    "SpecialPage",
]
