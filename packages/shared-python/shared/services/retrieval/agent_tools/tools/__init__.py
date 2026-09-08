"""Importing this package registers every ``corpus.*`` tool into ``REGISTRY``.

One module per tool, mirroring
``apps/worker/app/services/document_agent/tools/``.
"""

from __future__ import annotations

from shared.services.retrieval.agent_tools.tools import (
    assets as _assets,
    grep as _grep,
    list_documents as _list_documents,
    neighbors as _neighbors,
    node_filter as _node_filter,
    outline as _outline,
    read as _read,
    recall as _recall,
)

__all__ = [
    "_assets",
    "_grep",
    "_list_documents",
    "_neighbors",
    "_node_filter",
    "_outline",
    "_read",
    "_recall",
]
