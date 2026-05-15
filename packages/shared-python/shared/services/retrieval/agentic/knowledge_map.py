"""Knowledge-map overview for agentic document selection."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, GraphNode


_MAX_OVERVIEW_FILES = 50


async def build_knowledge_map_overview(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
) -> tuple[str, dict[str, str]]:
    """Build a file-level knowledge map overview for LLM file selection."""
    doc_stmt = (
        select(Document)
        .where(Document.user_id == user_id)
        .where(Document.namespace == namespace)
        .where(Document.status == "active")
        .where(Document.current_job_result_id.is_not(None))
        .order_by(Document.updated_at.desc())
        .limit(_MAX_OVERVIEW_FILES)
    )
    doc_result = await db.execute(doc_stmt)
    documents = list(doc_result.scalars())

    if not documents:
        return "(empty)", {}

    doc_ids = [document.document_id for document in documents]
    doc_id_to_name = {
        document.document_id: (document.source_file_name or document.document_id)
        for document in documents
    }

    chunk_stats_stmt = (
        select(
            DocumentChunk.document_id,
            func.count(DocumentChunk.id).label("chunk_count"),
            func.count(func.nullif(DocumentChunk.chunk_type, "text")).label("media_count"),
        )
        .join(
            Document,
            (Document.document_id == DocumentChunk.document_id)
            & (Document.current_job_result_id == DocumentChunk.job_result_id),
        )
        .where(DocumentChunk.document_id.in_(doc_ids))
        .group_by(DocumentChunk.document_id)
    )
    chunk_stats_result = await db.execute(chunk_stats_stmt)
    chunk_stats: dict[str, dict[str, int]] = {}
    for document_id, chunk_count, media_count in chunk_stats_result.all():
        chunk_stats[document_id] = {"total": chunk_count, "media": media_count}

    graph_summary_stmt = (
        select(GraphNode.owner_document_id, GraphNode.properties)
        .where(GraphNode.owner_document_id.in_(doc_ids))
        .where(GraphNode.node_kind == "document")
    )
    graph_summary_result = await db.execute(graph_summary_stmt)
    doc_top_summaries: dict[str, str] = {}
    for document_id, properties in graph_summary_result.all():
        if not isinstance(properties, dict):
            continue
        top_summary = str(properties.get("top_summary") or "").strip()
        if top_summary:
            doc_top_summaries[document_id] = top_summary

    lines: list[str] = []
    for document in documents:
        document_id = document.document_id
        name = doc_id_to_name[document_id]
        stats = chunk_stats.get(document_id, {"total": 0, "media": 0})
        top_summary = doc_top_summaries.get(document_id, "")

        line = f'- [{document_id}] {name}  chunks={stats["total"]}'
        if stats["media"] > 0:
            line += f' media={stats["media"]}'
        if top_summary:
            line += f"\n  top_summary:\n{indent_block(top_summary, 4)}"
        lines.append(line)

    return "\n".join(lines), doc_id_to_name


def indent_block(text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(f"{prefix}{line}" for line in str(text or "").splitlines())
