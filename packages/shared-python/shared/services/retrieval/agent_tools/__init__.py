"""Provider-agnostic corpus exploration tools.

See ``.cursor/plans/agentic_corpus_explore_retrieval_c2c4ea21.plan.md`` (Phase 2)
for the design. Tools in this package query the published, DB-served corpus
described in ``CORPUS_SCHEMA.md`` (``documents`` / ``document_sections`` /
``document_chunks`` / ``graph_nodes`` / ``graph_edges``) — not the on-disk
parse artifacts.

The same ``REGISTRY`` is meant to be consumed by two harnesses (Phase 3):
the API ``/mcp`` server (Cursor/Codex/Claude) and the in-process
``agent_explore`` tool-loop. Importing ``agent_tools.tools`` registers every
tool as a side effect; this package performs that import itself (below), so
any consumer importing a name here (e.g. ``REGISTRY``) is guaranteed a fully
populated registry.
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

# Register every ``corpus.*`` tool into ``REGISTRY`` as an import side effect:
# each module under ``tools/`` calls ``@register_tool`` at import time. Kept
# here — not in ``registry.py``, which the tool modules import and would
# therefore create a cycle — so consumers importing any name from this package
# no longer need their own side-effect import of ``tools``. ``_tools`` is
# intentionally never referenced (listed in ``__all__`` as a deliberate
# re-export so tooling does not flag it as unused).
from shared.services.retrieval.agent_tools import tools as _tools

__all__ = [
    "_tools",
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
