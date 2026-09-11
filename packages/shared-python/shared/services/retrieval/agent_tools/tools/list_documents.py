"""``corpus.list_documents`` — namespace-level document overview.

Joins ``documents`` with the document-level ``graph_nodes`` row (§4 of
``CORPUS_SCHEMA.md``) to surface per-document keywords/summary/type-mix
without reading any chunk content.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from shared.models.database.document import Document, GraphNode
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    register_tool,
)


@register_tool(
    name="corpus.list_documents",
    description=(
        "List every active document in the namespace with its parse_track "
        "and, when available, document-level graph metadata (top_keywords, "
        "top_summary, chunk type mix). Use this to start cold: which "
        "documents exist and what are they about, before picking one for "
        "outline/node_filter/recall/read."
    ),
    json_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
)
async def list_documents(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    stmt = (
        select(Document, GraphNode.properties)
        .outerjoin(
            GraphNode,
            (GraphNode.owner_document_id == Document.document_id)
            & (GraphNode.node_kind == "document"),
        )
        .where(Document.user_id == ctx.user_id)
        .where(Document.namespace == ctx.namespace)
        .where(Document.status == "active")
        .where(ctx.document_scope.predicate(Document.document_id))
        .order_by(Document.source_file_name)
    )
    rows = (await ctx.db.execute(stmt)).all()

    documents: list[dict[str, Any]] = []
    lines: list[str] = []
    for document, properties in rows:
        props = properties if isinstance(properties, dict) else {}
        entry = {
            "document_id": document.document_id,
            "source_file_name": document.source_file_name,
            "parse_track": document.parse_track,
            "top_keywords": props.get("top_keywords") or [],
            "top_summary": props.get("top_summary") or "",
            "types": props.get("types") or {},
            "chunks_count": props.get("chunks_count"),
        }
        documents.append(entry)
        summary_line = (
            f"- {entry['source_file_name']} ({entry['document_id']}, "
            f"track={entry['parse_track']}, chunks={entry['chunks_count']})"
        )
        if entry["top_summary"]:
            summary_line += f"\n  summary: {entry['top_summary']}"
        if entry["top_keywords"]:
            summary_line += f"\n  keywords: {', '.join(entry['top_keywords'])}"
        lines.append(summary_line)

    text = f"documents={len(documents)}\n" + "\n".join(lines) if documents else "documents=0"
    return ToolResult(
        text=text,
        payload={"documents": documents},
        refs=[{"document_id": doc["document_id"]} for doc in documents],
    )
