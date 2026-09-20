"""``corpus.recall`` — fuzzy ranked candidate search.

Two real channels today, fused by the existing RRF utility
(``search.scoring.merge_channels_rrf``, same ``RRF_K`` as every other RRF
fusion in retrieval):

- ``path_content``: reuses ``search.map_unit_discovery.map_unit_discovery``
  (persisted map-unit BM25 over path+content, already RRF-fused internally).
- ``term``: a fresh substring channel over
  ``document_map_units.term_search_text_lower`` — this column is persisted at
  index time and is ranked here as an independent substring channel.

``vector`` is accepted in ``channels`` but rejected as reserved/not
implemented (``CORPUS_SCHEMA.md`` §5) — it is not silently ignored.

Fusing an already-doubly-fused channel (path_content) with a fresh single
channel (term) at equal RRF weight is a necessary, disclosed design choice:
there is no persisted precedent for a different weight ratio between them
(the old 3-channel weights of path=1.0/content=2.0/term=1.5 no longer exist
in code — only path=1.0/content=2.0 survive in ``scoring.knowhere_hybrid``).

The term channel's snippet and the rendered ``text`` preview both go through
the shared ``agent_tools.snippet.build_snippet`` (head + first-match window +
tail, ``...``-joined, overlap-merged) — the same mechanism ``corpus.grep``
uses, so window-slicing constants live in one place.
"""

from __future__ import annotations

from shared.services.retrieval.document_scope import DocumentScope

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import DocumentChunk
from shared.services.retrieval.agent_tools.explore_mount import mount_explore_hits
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    capped_limit,
    register_tool,
)
from shared.services.retrieval.agent_tools.snippet import (
    build_snippet,
    format_search_hit_line,
)
from shared.services.retrieval.hydration.row_utils import normalize_chunk_type
from shared.services.retrieval.settings import ASSET_CHUNK_TYPES
from shared.services.retrieval.search.map_unit_discovery import map_unit_discovery
from shared.services.retrieval.search.scoring import merge_channels_rrf

_SUPPORTED_CHANNELS = {"path_content", "term"}
_RESERVED_CHANNELS = {"vector"}
_DEFAULT_TOP_K = 10

_TERM_CHANNEL_SQL = """
SELECT dmu.document_id, dmu.job_result_id, dmu.section_id, ds.section_path,
       d.source_file_name, dmu.term_search_text_lower, jr.job_id
FROM document_map_units dmu
JOIN documents d
    ON d.document_id = dmu.document_id
    AND d.current_job_result_id = dmu.job_result_id
JOIN document_sections ds ON ds.section_id = dmu.section_id
JOIN job_results jr ON jr.id = dmu.job_result_id
WHERE d.user_id = :user_id
    AND d.namespace = :namespace
    AND d.status = 'active'
    {doc_clause}
    AND dmu.term_search_text_lower LIKE :like_pattern
ORDER BY POSITION(:needle IN dmu.term_search_text_lower) ASC
LIMIT :limit
"""


async def _term_channel_rows(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    document_scope: DocumentScope,
    chunk_types: set[str] | None,
    top_k: int,
) -> list[dict[str, Any]]:
    needle = query.strip().lower()
    if not needle:
        return []
    params: dict[str, Any] = {
        "user_id": user_id,
        "namespace": namespace,
        "like_pattern": f"%{needle}%",
        "needle": needle,
        "limit": top_k,
    }
    doc_clause, scope_params = document_scope.sql()
    params.update(scope_params)
    statement = text(_TERM_CHANNEL_SQL.format(doc_clause=doc_clause))
    unit_rows = [dict(row._mapping) for row in (await db.execute(statement, params)).all()]
    if not unit_rows:
        return []

    keys = [
        (row["document_id"], row["job_result_id"], row["section_id"]) for row in unit_rows
    ]
    chunk_result = await db.execute(
        select(DocumentChunk).where(
            DocumentChunk.document_id.in_({k[0] for k in keys}),
            DocumentChunk.job_result_id.in_({k[1] for k in keys}),
            DocumentChunk.section_id.in_({k[2] for k in keys}),
        )
    )
    chunk_by_key = {
        (c.document_id, c.job_result_id, c.section_id): c
        for c in chunk_result.scalars().all()
    }

    results: list[dict[str, Any]] = []
    for unit_row in unit_rows:
        key = (unit_row["document_id"], unit_row["job_result_id"], unit_row["section_id"])
        chunk = chunk_by_key.get(key)
        if chunk is None:
            continue
        if chunk_types and chunk.chunk_type not in chunk_types:
            continue
        haystack = unit_row["term_search_text_lower"]
        needle_pos = haystack.find(needle)
        hit = (needle_pos, needle_pos + len(needle)) if needle_pos >= 0 else None
        results.append(
            {
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "section_id": chunk.section_id,
                "section_path": unit_row["section_path"],
                "source_file_name": unit_row["source_file_name"],
                "chunk_type": chunk.chunk_type,
                "snippet": build_snippet(haystack, hit),
                "content": chunk.content,
                "file_path": chunk.file_path,
                "chunk_metadata": chunk.chunk_metadata or {},
                "job_result_id": chunk.job_result_id,
                "job_id": unit_row["job_id"],
            }
        )
    return results


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
        "where the answer lives. Fuses a path+content BM25 channel with a "
        "term substring channel via RRF. Returns candidates with chunk_type, "
        "document_id, path and snippet. Table and image hits include rendered "
        "content; body hits include any connected table or image. Image/table "
        "hits also include chunk_id. Call corpus.read using document_id plus "
        "section_path or chunk_id, not the filename."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "document_ids": {"type": "array", "items": {"type": "string"}},
            "chunk_types": {"type": "array", "items": {"type": "string"}},
            "channels": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["path_content", "term", "vector"],
                },
                "default": ["path_content", "term"],
                "description": "'vector' is reserved and not implemented yet.",
            },
            "top_k": {"type": "integer", "default": _DEFAULT_TOP_K},
        },
        "required": ["query"],
    },
)
async def recall(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(text="", error="recall requires query")
    requested_top_k = int(args.get("top_k") or _DEFAULT_TOP_K)
    top_k = capped_limit(requested_top_k, ctx.budget)
    document_ids = [
        str(d).strip() for d in (args.get("document_ids") or []) if str(d).strip()
    ]
    chunk_types = {
        str(t).strip().lower() for t in (args.get("chunk_types") or []) if str(t).strip()
    } or None
    requested_channels = set(args.get("channels") or list(_SUPPORTED_CHANNELS))
    reserved_requested = requested_channels & _RESERVED_CHANNELS
    active_channels = requested_channels & _SUPPORTED_CHANNELS
    unknown_channels = requested_channels - _SUPPORTED_CHANNELS - _RESERVED_CHANNELS
    if unknown_channels:
        return ToolResult(text="", error=f"unsupported channels: {sorted(unknown_channels)}")
    if not active_channels:
        return ToolResult(
            text="",
            error="no runnable channels requested (vector is reserved, not implemented)",
        )

    document_scope = ctx.document_scope.narrow(document_ids) if document_ids else ctx.document_scope
    channel_rows: list[list[dict[str, Any]]] = []
    weights: list[float] = []

    if "path_content" in active_channels:
        discovery = await map_unit_discovery(
            ctx.db,
            user_id=ctx.user_id,
            namespace=ctx.namespace,
            query=query,
            top_k=top_k,
            exclude_document_ids=[],
            document_scope=document_scope,
            exclude_sections=[],
            chunk_types=chunk_types,
        )
        channel_rows.append(list(discovery.payload.get("fused_rows") or []))
        weights.append(1.0)

    if "term" in active_channels:
        term_rows = await _term_channel_rows(
            ctx.db,
            user_id=ctx.user_id,
            namespace=ctx.namespace,
            query=query,
            document_scope=document_scope,
            chunk_types=chunk_types,
            top_k=top_k,
        )
        channel_rows.append(term_rows)
        weights.append(1.0)

    fused = merge_channels_rrf(channel_rows, weights, top_k)
    media: list[dict[str, str]] = []
    if fused:
        fused, media = await mount_explore_hits(
            ctx, fused, char_budget=ctx.budget.max_chars
        )

    lines = [f"candidates={len(fused)}"]
    if len(fused) < 2:
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
    if reserved_requested:
        lines.append(f"note: channels {sorted(reserved_requested)} are reserved, not run")
    if requested_top_k > top_k:
        lines.append(f"note: capped to budget.max_items={ctx.budget.max_items}")
    for row in fused:
        snippet = _identifier_snippet(row)
        chunk_type = str(row.get("chunk_type") or "").strip()
        lines.append(
            format_search_hit_line(
                source_file_name=row.get("source_file_name"),
                document_id=row.get("document_id"),
                section_path=row.get("section_path"),
                snippet=snippet,
                chunk_type=chunk_type,
                chunk_id=(
                    row.get("chunk_id") if chunk_type in ASSET_CHUNK_TYPES else None
                ),
                score=row.get("score"),
            )
        )
        rendered = str(row.get("rendered") or "").strip()
        if rendered:
            lines.append(rendered)

    return ToolResult(
        text="\n".join(lines),
        payload={"candidates": fused, "reserved_channels": sorted(reserved_requested)},
        refs=[
            {"document_id": row.get("document_id"), "chunk_id": row.get("chunk_id")}
            for row in fused
        ],
        media=media,
    )
