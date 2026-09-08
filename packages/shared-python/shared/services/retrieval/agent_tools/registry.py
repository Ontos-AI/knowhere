"""Provider-agnostic tool contracts for corpus exploration.

Mirrors the shape of ``apps/worker/app/services/document_agent/registry.py``
(``ToolSpec`` + a decorator-based registry), adapted for the async DB-backed
corpus tools in this package: ``ToolSpec(name, description, json_schema, run)``,
``ToolContext(db, user_id, namespace, budget)``, ``ToolResult(text, payload, refs)``.

Both the API ``/mcp`` server and the in-process ``agent_explore`` tool-loop
(Phase 3) dispatch through the same ``REGISTRY`` — this module has no
provider-specific (MCP / OpenAI tool-calling) concerns.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.settings import EVIDENCE_TEXT_CHAR_BUDGET


@dataclass(frozen=True)
class ToolBudget:
    """Per-call output budget passed down to every tool via ``ToolContext``.

    ``max_items`` is a hard ceiling on how many rows a tool that returns an
    unbounded/ranked list (``recall``, ``grep``, asset forward search) may
    return in one call: each such tool keeps its own smaller, tool-appropriate
    default (e.g. ``grep``'s ``max_results``, ``recall``'s ``top_k``) but
    clamps the caller-requested value to this ceiling via ``capped_limit()``
    below, and notes it in ``ToolResult.text`` when the request was clamped.
    Tools that promise a complete, non-truncated set by contract
    (``node_filter``, ``outline``) do not apply this budget to their
    matched-set cardinality — see ``CORPUS_SCHEMA.md`` §6.

    ``max_chars`` caps the rendered ``ToolResult.text`` before it enters LLM
    context. Applied in ``agent_explore.episode._tool_message_content`` (not
    inside individual tools) so ``read`` can return full body text from the
    tool while the harness still bounds what the model sees per turn. Aligned
    with map-nav final evidence packing via ``EVIDENCE_TEXT_CHAR_BUDGET``
    (12_000).
    """

    max_chars: int = EVIDENCE_TEXT_CHAR_BUDGET
    max_items: int = 50


def capped_limit(requested: int, budget: ToolBudget) -> int:
    """Clamp a caller-requested row count to ``budget.max_items`` (min 1)."""
    return max(1, min(requested, budget.max_items))


@dataclass
class ToolContext:
    """Per-call execution context. One instance is built per tool dispatch."""

    db: AsyncSession
    user_id: str
    namespace: str
    budget: ToolBudget = field(default_factory=ToolBudget)


@dataclass
class ToolResult:
    """Uniform tool output.

    ``text`` is the human/LLM-facing rendering; ``payload`` is the structured
    data (for programmatic callers and for building ``refs``); ``refs`` are
    resolvable evidence pointers (``{document_id, section_path|chunk_id}``)
    that a harness can fold into ``referenced_chunks`` (Phase 3 bridge).
    ``error`` is set instead of raising for caller-facing input mistakes (bad
    args, unknown document_id) so a tool-loop agent can see and correct them.
    """

    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    refs: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    json_schema: dict[str, Any]
    run: ToolHandler


class ToolRegistry:
    """Name -> ``ToolSpec`` map. Provider-agnostic; no MCP/OpenAI coupling."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    async def dispatch(
        self, name: str, ctx: ToolContext, args: dict[str, Any]
    ) -> ToolResult:
        spec = self.get(name)
        if spec is None:
            return ToolResult(text="", error=f"unknown tool: {name}")
        return await spec.run(ctx, args)


REGISTRY = ToolRegistry()


def register_tool(
    *,
    name: str,
    description: str,
    json_schema: dict[str, Any],
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator mirroring worker's ``register_tool`` for the async corpus tools."""

    def _decorator(handler: ToolHandler) -> ToolHandler:
        REGISTRY.register(
            ToolSpec(
                name=name,
                description=description,
                json_schema=json_schema,
                run=handler,
            )
        )
        return handler

    return _decorator
