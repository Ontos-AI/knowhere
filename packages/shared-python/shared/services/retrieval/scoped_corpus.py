from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult
from shared.services.retrieval.section_filters import is_excluded_section


async def count_scoped_chunks(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    exclude_document_ids: list[str],
    allowed_chunk_types: set[str] | None,
) -> int:
    stmt = (
        select(func.count(DocumentChunk.id))
        .join(
            Document,
            (Document.document_id == DocumentChunk.document_id)
            & (Document.current_job_result_id == DocumentChunk.job_result_id),
        )
        .where(Document.user_id == user_id)
        .where(Document.namespace == namespace)
        .where(Document.status == 'active')
    )
    if exclude_document_ids:
        stmt = stmt.where(Document.document_id.notin_(list(exclude_document_ids)))
    if allowed_chunk_types is not None:
        stmt = stmt.where(func.lower(DocumentChunk.chunk_type).in_(list(allowed_chunk_types)))
    result = await db.execute(stmt)
    return result.scalar() or 0


async def load_all_scoped_chunks(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    allowed_chunk_types: set[str] | None,
    signal_paths: list[str],
    filter_mode: str,
) -> list[dict[str, Any]]:
    stmt = (
        select(Document, DocumentChunk, DocumentSection, JobResult)
        .join(
            DocumentChunk,
            (DocumentChunk.document_id == Document.document_id)
            & (DocumentChunk.job_result_id == Document.current_job_result_id),
        )
        .outerjoin(DocumentSection, DocumentSection.section_id == DocumentChunk.section_id)
        .join(JobResult, JobResult.id == DocumentChunk.job_result_id)
        .where(Document.user_id == user_id)
        .where(Document.namespace == namespace)
        .where(Document.status == 'active')
        .order_by(DocumentChunk.sort_order)
    )
    if exclude_document_ids:
        stmt = stmt.where(Document.document_id.notin_(list(exclude_document_ids)))
    if allowed_chunk_types is not None:
        stmt = stmt.where(func.lower(DocumentChunk.chunk_type).in_(list(allowed_chunk_types)))

    result = await db.execute(stmt)
    rows: list[dict[str, Any]] = []
    for document, chunk, section, job_result in result.all():
        section_path = section.section_path if section else None
        if is_excluded_section(
            document_id=document.document_id,
            section_path=section_path,
            exclude_sections=exclude_sections,
        ):
            continue
        if signal_paths and section_path:
            path_lower = section_path.lower()
            matches_any = any(keyword.lower() in path_lower for keyword in signal_paths)
            if filter_mode == 'keep' and not matches_any:
                continue
            if filter_mode == 'delete' and matches_any:
                continue
        rows.append({
            'document_id': document.document_id,
            'chunk_id': chunk.chunk_id,
            'section_id': chunk.section_id,
            'section_path': section_path,
            'source_file_name': document.source_file_name,
            'chunk_type': chunk.chunk_type,
            'content': chunk.content,
            'score': 1.0,
            'file_path': chunk.file_path,
            'chunk_metadata': chunk.chunk_metadata or {},
            'job_result_id': chunk.job_result_id,
            'job_id': job_result.job_id if job_result else None,
            'sort_order': chunk.sort_order,
        })
    return rows
