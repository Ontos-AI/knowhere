from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult
from shared.services.retrieval.search.section_filters import is_excluded_section

_SECTION_EXCLUSION_PAGE_MULTIPLIER = 2


def _build_lexical_match_predicate(query: str):
    like = f'%{query}%'
    return (
        DocumentChunk.content_lexical_text.ilike(like)
        | DocumentChunk.path_lexical_text.ilike(like)
    )


class GraphQueryService:
    """Read-side graph routing before canonical chunk hydration."""

    async def find_entry_documents(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        query: str,
        exclude_document_ids: Iterable[str] = (),
        exclude_sections: Iterable[dict[str, str]] = (),
    ) -> list[str]:
        query_lc = query.lower().strip()
        excluded_document_ids = set(exclude_document_ids)

        if query_lc:
            section_matches = await self._find_documents_by_section(
                db,
                user_id=user_id,
                namespace=namespace,
                query=query_lc,
                exclude_document_ids=excluded_document_ids,
                exclude_sections=exclude_sections,
            )
            if section_matches:
                return section_matches

        return await self._find_documents_by_content(
            db,
            user_id=user_id,
            namespace=namespace,
            query=query_lc,
            exclude_document_ids=excluded_document_ids,
        )

    async def _find_documents_by_section(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        query: str,
        exclude_document_ids: set[str],
        exclude_sections: Iterable[dict[str, str]],
    ) -> list[str]:
        like = f'%{query}%'
        stmt = (
            select(DocumentSection.document_id)
            .join(
                Document,
                (Document.document_id == DocumentSection.document_id)
                & (Document.current_job_result_id == DocumentSection.job_result_id),
            )
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(Document.status == 'active')
            .where(
                DocumentSection.section_title.ilike(like)
                | DocumentSection.section_path.ilike(like)
            )
            .distinct()
        )
        if exclude_document_ids:
            stmt = stmt.where(Document.document_id.notin_(list(exclude_document_ids)))
        for item in exclude_sections or ():
            if not isinstance(item, dict):
                continue
            excluded_document_id = str(item.get('document_id') or '').strip()
            excluded_path = str(item.get('section_path') or '').strip()
            if excluded_document_id and excluded_path:
                stmt = stmt.where(
                    ~(
                        (DocumentSection.document_id == excluded_document_id)
                        & (
                            (DocumentSection.section_path == excluded_path)
                            | DocumentSection.section_path.like(f'{excluded_path} / %')
                        )
                    )
                )

        result = await db.execute(stmt)
        return [document_id for (document_id,) in result.all()]

    async def _find_documents_by_content(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        query: str,
        exclude_document_ids: set[str],
    ) -> list[str]:
        like = f'%{query}%'
        stmt = (
            select(Document.document_id)
            .join(
                DocumentChunk,
                (DocumentChunk.document_id == Document.document_id)
                & (DocumentChunk.job_result_id == Document.current_job_result_id),
            )
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(Document.status == 'active')
            .where(DocumentChunk.content_lexical_text.ilike(like))
        )
        if exclude_document_ids:
            stmt = stmt.where(Document.document_id.notin_(list(exclude_document_ids)))
        result = await db.execute(stmt)
        seen: list[str] = []
        for (document_id,) in result.all():
            if document_id and document_id not in seen:
                seen.append(document_id)
        return seen

    async def collect_candidate_chunks(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        entry_document_ids: Sequence[str],
        query: str,
        top_k: int,
        exclude_sections: Iterable[dict[str, str]] = (),
    ) -> list[dict[str, Any]]:
        if not entry_document_ids:
            return []
        page_size = top_k
        if exclude_sections:
            page_size = max(top_k, top_k * _SECTION_EXCLUSION_PAGE_MULTIPLIER)
        base_stmt = (
            select(Document, DocumentChunk, DocumentSection, JobResult)
            .join(
                DocumentChunk,
                (DocumentChunk.document_id == Document.document_id)
                & (DocumentChunk.job_result_id == Document.current_job_result_id),
            )
            .outerjoin(
                DocumentSection,
                DocumentSection.section_id == DocumentChunk.section_id,
            )
            .join(JobResult, JobResult.id == DocumentChunk.job_result_id)
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(Document.status == 'active')
            .where(Document.document_id.in_(list(entry_document_ids)))
            .where(_build_lexical_match_predicate(query))
            .order_by(DocumentChunk.sort_order)
        )
        rows: list[dict[str, Any]] = []
        offset = 0
        while len(rows) < top_k:
            result = await db.execute(base_stmt.limit(page_size).offset(offset))
            result_rows = result.all()
            if not result_rows:
                break
            for document, chunk, section, job_result in result_rows:
                section_path = section.section_path if section else None
                if is_excluded_section(
                    document_id=document.document_id,
                    section_path=section_path,
                    exclude_sections=exclude_sections,
                ):
                    continue
                rows.append({
                    'document_id': document.document_id,
                    'chunk_id': chunk.chunk_id,
                    'section_id': chunk.section_id,
                    'section_path': section_path,
                    'source_file_name': document.source_file_name,
                    'chunk_type': chunk.chunk_type,
                    'content': chunk.content,
                    'score': 2.0,
                    'file_path': chunk.file_path,
                    'chunk_metadata': chunk.chunk_metadata or {},
                    'job_result_id': chunk.job_result_id,
                    'job_id': job_result.job_id if job_result else None,
                })
                if len(rows) >= top_k:
                    break
            if len(result_rows) < page_size:
                break
            offset += page_size
        return rows
