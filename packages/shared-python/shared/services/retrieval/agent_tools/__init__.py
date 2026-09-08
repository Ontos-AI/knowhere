"""Provider-agnostic corpus exploration tools.

See ``.cursor/plans/agentic_corpus_explore_retrieval_c2c4ea21.plan.md`` (Phase 2)
for the design. Tools in this package query the published, DB-served corpus
described in ``CORPUS_SCHEMA.md`` (``documents`` / ``document_sections`` /
``document_chunks`` / ``graph_nodes`` / ``graph_edges``) — not the on-disk
parse artifacts.

The same ``REGISTRY`` is meant to be consumed by two harnesses (Phase 3):
the API ``/mcp`` server (Cursor/Codex/Claude) and the in-process
``agent_explore`` tool-loop. Importing ``agent_tools.tools`` registers every
tool as a side effect.
"""

from __future__ import annotations

from shared.services.retrieval.agent_tools.registry import (
    REGISTRY,
    ToolBudget,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    capped_limit,
    register_tool,
)
from shared.services.retrieval.agent_tools.schema_doc import load_corpus_schema_text

__all__ = [
    "REGISTRY",
    "ToolBudget",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "capped_limit",
    "load_corpus_schema_text",
    "register_tool",
]
