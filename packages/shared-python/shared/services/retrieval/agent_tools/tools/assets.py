"""``corpus.assets`` — forward asset search and reverse asset -> hosts lookup.

Image/table chunks are parked under their document's synthetic ``Root``
section in the DB (§3 of ``CORPUS_SCHEMA.md``); the real association to a
body section lives in ``chunk_metadata.connect_to`` on the *body* chunk, not
on the asset. There is no stored asset -> body back-link, so the reverse
lookup (``host_of``) scans the candidate documents' text/page chunks in
Python and checks ``connect_to`` for the requested target ids.

``chunk_metadata`` is a plain ``JSON`` column (not ``JSONB``), so a
containment query (``@>``) is not available here — that operator is
JSONB-only in PostgreSQL.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    register_tool,
)
from shared.services.retrieval.hydration.row_utils import iter_connected_target_ids
from shared.services.retrieval.settings import ASSET_CHUNK_TYPES

_BODY_CHUNK_TYPES = ("text", "page")


@register_tool(
    name="corpus.assets",
    description=(
        "Forward search for image/table chunks by type/query, or reverse "
        "lookup: given asset chunk_ids (host_of), find which body "
        "section(s) embed or reference them via connect_to."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "document_ids": {"type": "array", "items": {"type": "string"}},
            "type": {
                "type": "string",
                "enum": ["image", "table", "any"],
                "default": "any",
            },
            "query": {
                "type": "string",
                "description": "Substring match against summary/keywords (forward search only).",
            },
            "host_of": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Asset chunk_ids to reverse-resolve to hosting sections.",
            },
        },
        "required": [],
    },
)
async def assets(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    document_ids = [
        str(d).strip() for d in (args.get("document_ids") or []) if str(d).strip()
    ]
    host_of = [str(c).strip() for c in (args.get("host_of") or []) if str(c).strip()]

    scope_filters: list[Any] = [
        ctx.document_scope.predicate(Document.document_id),
        Document.user_id == ctx.user_id,
        Document.namespace == ctx.namespace,
        Document.status == "active",
        Document.current_job_result_id == DocumentChunk.job_result_id,
    ]
    if document_ids:
        scope_filters.append(Document.document_id.in_(document_ids))

    if host_of:
        return await _reverse_lookup(ctx, scope_filters=scope_filters, target_ids=host_of)
    return await _forward_search(
        ctx,
        scope_filters=scope_filters,
        asset_type=str(args.get("type") or "any").strip().lower(),
        query=str(args.get("query") or "").strip().lower(),
    )


async def _forward_search(
    ctx: ToolContext,
    *,
    scope_filters: list[Any],
    asset_type: str,
    query: str,
) -> ToolResult:
    types = {asset_type} if asset_type in ASSET_CHUNK_TYPES else set(ASSET_CHUNK_TYPES)
    stmt = (
        select(DocumentChunk, DocumentSection.section_path, Document.source_file_name)
        .select_from(DocumentChunk)
        .join(Document, Document.document_id == DocumentChunk.document_id)
        .outerjoin(DocumentSection, DocumentSection.section_id == DocumentChunk.section_id)
        .where(*scope_filters)
        .where(DocumentChunk.chunk_type.in_(sorted(types)))
        .order_by(DocumentChunk.document_id, DocumentChunk.sort_order)
    )
    rows = (await ctx.db.execute(stmt)).all()

    results: list[dict[str, Any]] = []
    for chunk, section_path, source_file_name in rows:
        metadata = chunk.chunk_metadata if isinstance(chunk.chunk_metadata, dict) else {}
        summary = str(metadata.get("summary") or "").strip()
        keywords = metadata.get("keywords") or []
        if query:
            haystack = " ".join(
                [summary.lower(), " ".join(str(k).lower() for k in keywords)]
            )
            if query not in haystack:
                continue
        results.append(
            {
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "source_file_name": source_file_name,
                "chunk_type": chunk.chunk_type,
                "file_path": chunk.file_path,
                "summary": summary,
                "keywords": keywords,
                "section_path": section_path,
            }
        )
        if len(results) >= ctx.budget.max_items:
            break

    lines = [f"assets={len(results)}"]
    for r in results:
        lines.append(f"- [{r['chunk_type']}] {r['file_path']} — {r['summary']}")

    return ToolResult(
        text="\n".join(lines),
        payload={"assets": results},
        refs=[{"document_id": r["document_id"], "chunk_id": r["chunk_id"]} for r in results],
    )


async def _reverse_lookup(
    ctx: ToolContext,
    *,
    scope_filters: list[Any],
    target_ids: list[str],
) -> ToolResult:
    target_set = set(target_ids)
    stmt = (
        select(DocumentChunk, DocumentSection.section_path, Document.source_file_name)
        .select_from(DocumentChunk)
        .join(Document, Document.document_id == DocumentChunk.document_id)
        .outerjoin(DocumentSection, DocumentSection.section_id == DocumentChunk.section_id)
        .where(*scope_filters)
        .where(DocumentChunk.chunk_type.in_(_BODY_CHUNK_TYPES))
    )
    rows = (await ctx.db.execute(stmt)).all()

    hosts_by_target: dict[str, list[dict[str, Any]]] = {tid: [] for tid in target_set}
    for chunk, section_path, source_file_name in rows:
        row = {"chunk_metadata": chunk.chunk_metadata}
        for target_id in iter_connected_target_ids(row):
            if target_id in target_set:
                hosts_by_target[target_id].append(
                    {
                        "document_id": chunk.document_id,
                        "source_file_name": source_file_name,
                        "section_path": section_path,
                        "chunk_id": chunk.chunk_id,
                        "chunk_type": chunk.chunk_type,
                    }
                )

    lines = []
    for target_id, hosts in hosts_by_target.items():
        if not hosts:
            lines.append(f"- {target_id}: no host found (unresolved Root asset)")
            continue
        for host in hosts:
            lines.append(
                f"- {target_id} <- {host['source_file_name']} / {host['section_path']}"
            )

    return ToolResult(
        text="\n".join(lines) if lines else "no hosts found",
        payload={"hosts_by_target": hosts_by_target},
        refs=[
            {"document_id": host["document_id"], "section_path": host["section_path"]}
            for hosts in hosts_by_target.values()
            for host in hosts
        ],
    )
