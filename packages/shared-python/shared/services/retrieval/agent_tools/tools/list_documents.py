"""``corpus.list_documents`` — namespace-level document overview.

Joins ``documents`` with the document-level ``graph_nodes`` row
to surface per-document keywords/summary/type-mix
without reading any chunk content.
"""

from __future__ import annotations

from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.corpus_revision_context import CorpusRevisionContext

from typing import Any

from sqlalchemy import select

from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    register_tool,
)


@register_tool(
    name="corpus.list_documents",
    description=(
        "List every active document in the namespace with its summary, "
        "top keywords and chunk count. Use only when explicitly asked to "
        "inventory the namespace's documents, not as a first step for "
        "question answering."
    ),
    json_schema={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
)
async def list_documents(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(ctx.namespace)
    stmt = (
        select(corpusStorage.Document, corpusStorage.GraphNode.properties)
        .outerjoin(
            corpusStorage.GraphNode,
            (corpusStorage.GraphNode.owner_document_id == corpusStorage.Document.document_id)
            & (corpusStorage.GraphNode.node_kind == "document")
            & (corpusStorage.GraphNode.job_result_id == CorpusRevisionContext.build_revision_column(corpusStorage.Document)),
        )
        .where(corpusStorage.Document.user_id == corpusStorage.resolve_owner(ctx.user_id))
        .where(corpusStorage.Document.namespace == ctx.namespace)
        .where(corpusStorage.Document.status == "active")
        .where(ctx.document_scope.predicate(corpusStorage.Document.document_id))
        .order_by(corpusStorage.Document.source_file_name)
    )
    rows = (await ctx.db.execute(stmt)).all()

    documents: list[dict[str, Any]] = []
    lines: list[str] = []
    for document, properties in rows:
        props = properties if isinstance(properties, dict) else {}
        entry = {
            "document_id": document.document_id,
            "job_result_id": CorpusRevisionContext.resolve_revision(document.document_id, document.current_job_result_id),
            "source_file_name": document.source_file_name,
            "top_keywords": props.get("top_keywords") or [],
            "top_summary": props.get("top_summary") or "",
            "types": props.get("types") or {},
            "chunks_count": props.get("chunks_count"),
        }
        documents.append(entry)
        summary_line = (
            f"- {entry['source_file_name']} ({entry['document_id']}, "
            f"chunks={entry['chunks_count']})"
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
