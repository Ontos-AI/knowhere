"""Document Anatomy Agent package.

``DocumentAnatomyAgent`` produces a ``PageMap`` before any PDF parsing begins,
enabling semantically-correct shard decisions for large documents.
"""

from app.services.document_agent.agent import DocumentAnatomyAgent
from app.services.document_agent.page_map import (
    CutPoint,
    H1BoundaryResult,
    H1Match,
    PageFeature,
    PageMap,
    Shard,
)

__all__ = [
    "DocumentAnatomyAgent",
    "CutPoint",
    "H1BoundaryResult",
    "H1Match",
    "PageFeature",
    "PageMap",
    "Shard",
]
