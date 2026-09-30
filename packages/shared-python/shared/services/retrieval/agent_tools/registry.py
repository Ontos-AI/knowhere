"""Provider-agnostic tool contracts for corpus exploration.

Mirrors the shape of ``apps/worker/app/services/document_agent/registry.py``
(``ToolSpec`` + a decorator-based registry), adapted for the async DB-backed
corpus tools in this package: ``ToolSpec(name, description, json_schema, run)``,
``ToolContext(db, user_id, namespace, db_factory, budget)``,
``ToolResult(text, payload, refs, media)``.

Both the API ``/mcp`` server and the in-process ``agent_explore`` tool-loop
(Phase 3) dispatch through the same ``REGISTRY`` — this module has no
provider-specific (MCP / OpenAI tool-calling) concerns.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Set
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

import jsonschema
import jsonschema.validators
from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.document_scope import DocumentScope
from shared.services.retrieval.settings import EVIDENCE_TEXT_CHAR_BUDGET

DbFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

logger = logging.getLogger(__name__)

# Char budget for the two map tools (``corpus.outline``, ``corpus.node_filter``)
# once they self-bound via lighting instead of a hard mid-text cut — see
# ``scoring/map_lighting.py`` and each harness's ``tool_message_content`` call.
# Every other tool still uses ``EVIDENCE_TEXT_CHAR_BUDGET`` below.
MAP_TOOL_CHAR_BUDGET = 20_000

CHUNK_TYPE_VALUES = ("text", "page", "image", "table")


# ``oneOf`` for a ``{document_id, section_path | chunk_id}`` ref item: exactly
# one address. The item schema's ``description`` is the validation message.
REF_ADDRESS_ONE_OF: list[dict[str, Any]] = [
    {"required": ["section_path"], "not": {"required": ["chunk_id"]}},
    {"required": ["chunk_id"], "not": {"required": ["section_path"]}},
]
REF_ADDRESS_RULE = "each ref needs document_id plus exactly one of section_path or chunk_id"


def chunk_types_schema(description: str) -> dict[str, Any]:
    """``chunk_types`` argument schema shared by every tool that filters on it."""
    return {
        "type": "array",
        "items": {"type": "string", "enum": list(CHUNK_TYPE_VALUES)},
        "minItems": 1,
        "uniqueItems": True,
        "description": description,
    }


@dataclass(frozen=True)
class ToolBudget:
    """Per-call output budget passed down to every tool via ``ToolContext``.

    ``max_items`` is a hard ceiling on how many rows a tool that returns an
    unbounded/ranked list (``recall``, ``grep``, asset forward search) may
    return in one call: each such tool keeps its own smaller, tool-appropriate
    default (both ``grep`` and ``recall`` call this argument ``limit``) but
    clamps the caller-requested value to this ceiling via ``capped_limit()``
    below, and notes it in ``ToolResult.text`` when the request was clamped.
    Tools that promise a complete, non-truncated set by contract
    (``node_filter``, ``outline``) do not apply this budget to their
    matched-set cardinality.

    ``max_chars`` caps the rendered ``ToolResult.text`` before it enters LLM
    context. Applied in ``agent_explore.shared.tool_message_content`` (not
    inside individual tools) so ``read`` can return full body text from the
    tool while the harness still bounds what the model sees per turn. Aligned
    with final evidence packing via ``EVIDENCE_TEXT_CHAR_BUDGET``
    (12_000).
    """

    max_chars: int = EVIDENCE_TEXT_CHAR_BUDGET
    max_items: int = 50


def capped_limit(requested: int, budget: ToolBudget) -> int:
    """Clamp a caller-requested row count down to ``budget.max_items``.

    The lower bound is the tool schema's ``minimum`` — a non-positive
    request fails validation instead of being raised to 1 here.
    """
    return min(requested, budget.max_items)


# ``(document_id, section_path)`` and ``(document_id, chunk_id)`` addresses
# an ``agent_explore`` episode received in results of earlier rounds.
ReadableAddresses = Set[tuple[str, str]]


@dataclass(frozen=True)
class Decision:
    """One body chunk an ``agent_explore`` episode read and then picked or not."""

    read_round: int
    picked_handle: str | None
    handle: str


@dataclass
class ToolContext:
    """Per-call execution context. One instance is built per tool dispatch."""

    db: AsyncSession
    user_id: str
    namespace: str
    db_factory: DbFactory
    budget: ToolBudget = field(default_factory=ToolBudget)
    document_scope: DocumentScope = DocumentScope()
    # The end user's original query for this episode, when the caller is
    # ``agent_explore`` (``dispatch.dispatch_tool_call`` threads it through
    # from ``run_episode``). Used only by ``corpus.outline``/``corpus.node_filter``
    # to score and fold an oversized map (``scoring/map_lighting.py``) — never
    # by hit tools, which already take their own ``query``/``pattern``.
    # Empty when a caller (e.g. an external MCP client hitting a single
    # ``corpus.*`` tool directly) has no such query: an oversized map that
    # dropping summaries does not fit then fails that one call as "too
    # large to map" instead of guessing.
    query: str = ""
    # ``corpus.read`` gates, set only by ``agent_explore``. ``None`` means no
    # gate: an external MCP client calling one tool has no episode.
    readable: ReadableAddresses | None = None
    decided: Mapping[tuple[str, str], Decision] | None = None


@dataclass
class ToolResult:
    """Uniform tool output.

    ``text`` is the human/LLM-facing rendering; ``payload`` is the structured
    data (for programmatic callers and for building ``refs``); ``refs`` are
    resolvable evidence pointers (``{document_id, section_path|chunk_id}``)
    that a harness can fold into ``referenced_chunks`` (Phase 3 bridge).
    ``error`` is set instead of raising for caller-facing input mistakes (bad
    args, unknown document_id) so a tool-loop agent can see and correct them.
    ``media`` is optional HTTPS image URLs for a vision-capable harness.
    """

    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    refs: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    media: list[dict[str, str]] = field(default_factory=list)


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
        schema_error = validate_tool_args(spec, args)
        if schema_error is not None:
            return ToolResult(text="", error=schema_error)
        try:
            return await spec.run(ctx, args)
        except Exception as exc:  # noqa: BLE001 - one broken call must not end the caller's loop
            logger.exception("corpus tool %s raised", name)
            return ToolResult(
                text="",
                error=(
                    f"{name}: internal error ({type(exc).__name__}: {exc}). Only "
                    "this call failed — retry with a narrower scope, or use "
                    "another corpus.* tool."
                ),
            )


def _describe_error_path(path: Any) -> str:
    parts: list[str] = []
    for part in path:
        if isinstance(part, int):
            parts.append(f"[{part}]")
        else:
            parts.append(f".{part}" if parts else str(part))
    return "".join(parts) or "<top level>"


def validate_tool_args(spec: ToolSpec, args: dict[str, Any]) -> str | None:
    """Validate ``args`` against ``spec.json_schema``; ``None`` means valid.

    Every tool schema declares ``additionalProperties: false`` at every
    object level (registered tools are expected to keep this true — see
    each tool schema), so an unknown key anywhere in the argument
    tree fails here instead of being silently dropped or ignored downstream.
    The returned message names the offending path, the tool's legal
    top-level argument names, and — for an unknown-key error — the exact
    legal names at that nesting level, so the caller can retry correctly
    without re-reading the full schema.
    """
    if not isinstance(args, dict):
        return f"{spec.name}: arguments must be a JSON object, got {type(args).__name__}"

    validator_cls = jsonschema.validators.validator_for(spec.json_schema)
    validator = validator_cls(spec.json_schema)
    errors = sorted(
        validator.iter_errors(args), key=lambda error: [str(p) for p in error.path]
    )
    if not errors:
        return None

    error = errors[0]
    path = _describe_error_path(error.path)
    top_level_args = sorted((spec.json_schema.get("properties") or {}).keys())

    if error.validator == "additionalProperties":
        instance = error.instance if isinstance(error.instance, dict) else {}
        schema_here = error.schema if isinstance(error.schema, dict) else {}
        legal_here = sorted((schema_here.get("properties") or {}).keys())
        extra = sorted(set(instance) - set(legal_here))
        return (
            f"{spec.name}: unknown argument(s) {extra} at {path} — this "
            f"object only accepts: {legal_here}. Retry with only those names."
        )
    if error.validator == "required":
        return (
            f"{spec.name}: {error.message} at {path}. "
            f"Top-level arguments: {top_level_args}."
        )
    if error.validator == "oneOf":
        rule = error.schema.get("description") if isinstance(error.schema, dict) else None
        return (
            f"{spec.name}: invalid argument at {path}: {rule or error.message}. "
            f"Top-level arguments: {top_level_args}."
        )
    return (
        f"{spec.name}: invalid argument at {path}: {error.message}. "
        f"Top-level arguments: {top_level_args}."
    )


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
