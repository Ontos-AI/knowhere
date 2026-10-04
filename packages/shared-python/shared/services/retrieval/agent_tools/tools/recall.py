"""``corpus.recall`` — fuzzy ranked candidate search.

One live scoring path: ``search.map_unit_discovery.map_unit_discovery``
(persisted map-unit BM25 over path + content, already RRF-fused there).
Exact-string lookup is ``corpus.grep``, not a second channel here.

``scope`` is applied in that same discovery SQL: a named section is that
section and everything under it, scored before ranking. Classic retrieval
does not pass section targets, so its SQL is unchanged.

Rows render through the shared ``agent_tools.snippet.format_row`` — the
same row shape ``corpus.outline``/``corpus.node_filter``/``corpus.grep``/
``corpus.assets`` use.
"""

from __future__ import annotations

from shared.services.retrieval.corpus_revision_context import CorpusRevisionContext

from typing import Any

from shared.services.retrieval.agent_tools.asset_hosts import host_paths_for_hits
from shared.services.retrieval.agent_tools.explore_mount import mount_explore_hits
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    capped_limit,
    chunk_types_schema,
    register_tool,
)
from shared.services.retrieval.agent_tools.scope import (
    SCOPE_SCHEMA,
    ScopeTarget,
    resolve_scope,
    scope_document_ids,
)
from shared.services.retrieval.agent_tools.snippet import build_row, build_snippet, format_row
from shared.services.retrieval.hydration.row_utils import normalize_chunk_type
from shared.services.retrieval.settings import ASSET_CHUNK_TYPES
from shared.services.retrieval.search.map_unit_discovery import map_unit_discovery

_DEFAULT_LIMIT = 10


def _identifier_snippet(row: dict[str, Any]) -> str:
    """Body/image fall back to content; tables never use the stored path."""
    snippet = str(row.get("snippet") or "").strip()
    if snippet:
        return snippet
    if normalize_chunk_type(row.get("chunk_type")) == "table":
        metadata = row.get("chunk_metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        summary = str(metadata.get("summary") or "").strip()
        return build_snippet(summary) if summary else ""
    return build_snippet(str(row.get("content") or ""))


@register_tool(
    name="corpus.recall",
    description=(
        "Fuzzy ranked candidate search for a question when you don't know "
        "where the answer lives. Scores path and content with BM25. "
        "Returns candidates with chunk_type, document_id, path and snippet. "
        "Table and image hits include rendered content; body hits include "
        "any connected table or image. Exact string / identifier lookup is "
        "corpus.grep, not this tool. Read the candidate's document_id + "
        "section_path (or chunk_id, for image/table hits) with corpus.read "
        "next, not the filename."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The fuzzy question to rank path and content against.",
            },
            "scope": SCOPE_SCHEMA,
            "chunk_types": chunk_types_schema(
                "Restrict hits to these chunk types. Old type names are rejected."
            ),
            "limit": {
                "type": "integer",
                "minimum": 1,
                "default": _DEFAULT_LIMIT,
                "description": "Max ranked candidates to return.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)
async def recall(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(text="", error="recall requires query")
    requested_limit = int(args.get("limit", _DEFAULT_LIMIT))
    limit = capped_limit(requested_limit, ctx.budget)
    chunk_types = {
        str(t).strip().lower() for t in (args.get("chunk_types") or []) if str(t).strip()
    } or None

    scope: list[ScopeTarget] = []
    if args.get("scope") is not None:
        scope, scope_error = await resolve_scope(
            ctx.db,
            user_id=ctx.user_id,
            namespace=ctx.namespace,
            document_scope=ctx.document_scope,
            raw_scope=args.get("scope"),
        )
        if scope_error is not None:
            return ToolResult(text="", error=f"recall: {scope_error}")

    document_scope = (
        ctx.document_scope.narrow(scope_document_ids(scope)) if scope else ctx.document_scope
    )
    discovery = await map_unit_discovery(
        ctx.db,
        user_id=ctx.user_id,
        namespace=ctx.namespace,
        query=query,
        top_k=limit,
        exclude_document_ids=[],
        document_scope=document_scope,
        exclude_sections=[],
        chunk_types=chunk_types,
        revision_pins=CorpusRevisionContext.get_pins(),
        section_targets=[
            (target.document_id, target.section_path) for target in scope
        ]
        or None,
    )
    rows = list(discovery.payload.get("fused_rows") or [])

    media: list[dict[str, str]] = []
    if rows:
        rows, media = await mount_explore_hits(
            ctx, rows, char_budget=ctx.budget.max_chars
        )
    host_paths = await host_paths_for_hits(ctx, rows, scope)

    payload_rows: list[dict[str, Any]] = []
    lines = [f"candidates={len(rows)}"]
    if len(rows) < 2:
        # No tool name named here on purpose — this fires on *every* weak
        # recall regardless of what a better next step happens to be for
        # this corpus/query, so it nudges the agent to change approach
        # without prescribing which other tool to reach for (that's already
        # covered generically in CORPUS_SCHEMA.md §6's tool-selection table).
        lines.append(
            "note: few or no candidates for this phrasing — rephrasing the "
            "query and calling recall again rarely surfaces more; a "
            "different exploration approach is more likely to help than "
            "repeating recall with synonyms."
        )
    if requested_limit > limit:
        lines.append(f"note: capped to budget.max_items={ctx.budget.max_items}")
    for hit in rows:
        snippet = _identifier_snippet(hit)
        chunk_type = str(hit.get("chunk_type") or "").strip()
        is_asset = chunk_type in ASSET_CHUNK_TYPES
        key = (str(hit.get("document_id") or ""), str(hit.get("chunk_id") or ""))
        section_path, hosted = (
            host_paths[key]
            if is_asset and key in host_paths
            else (hit.get("section_path"), None)
        )
        raw_score = hit.get("score")
        row = build_row(
            kind=chunk_type or "text",
            document_id=hit.get("document_id"),
            section_path=section_path,
            title=hit.get("source_file_name"),
            chunk_id=hit.get("chunk_id") if is_asset else None,
            snippet=snippet,
            score=float(raw_score) if raw_score is not None else None,
            hosted=hosted if is_asset else None,
        )
        payload_rows.append(row)
        lines.append(format_row(row))
        rendered = str(hit.get("rendered") or "").strip()
        if rendered:
            lines.append(rendered)

    return ToolResult(
        text="\n".join(lines),
        payload={
            "rows": payload_rows,
            "details": {"capped": requested_limit > limit},
        },
        refs=[
            {"document_id": hit.get("document_id"), "chunk_id": hit.get("chunk_id")}
            for hit in rows
        ],
        media=media,
    )
