"""
Document data access for retrieval document lifecycle flows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult

DocumentChunkRow = tuple[DocumentChunk, DocumentSection | None, JobResult]


class DocumentRepository:
    async def list_by_user_namespace(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
    ) -> Sequence[Document]:
        result = await db.execute(
            select(Document)
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(Document.status != "archived")
            .order_by(Document.updated_at.desc())
        )
        return result.scalars().all()

    async def get_document(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        user_id: str,
    ) -> Document | None:
        result = await db.execute(
            select(Document)
            .where(Document.document_id == document_id)
            .where(Document.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def archive_document(
        self,
        db: AsyncSession,
        *,
        document: Document,
    ) -> Document:
        document.status = "archived"
        document.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
        return document

    async def count_current_document_chunks(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
        chunk_type: str | None = None,
    ) -> int:
        stmt = (
            select(func.count(DocumentChunk.id))
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
        )
        if chunk_type is not None:
            stmt = stmt.where(func.lower(DocumentChunk.chunk_type) == chunk_type)

        result = await db.execute(stmt)
        return int(result.scalar_one())

    async def list_current_document_chunks(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
        limit: int,
        offset: int,
        chunk_type: str | None = None,
    ) -> Sequence[DocumentChunkRow]:
        stmt = (
            select(DocumentChunk, DocumentSection, JobResult)
            .outerjoin(
                DocumentSection,
                DocumentSection.section_id == DocumentChunk.section_id,
            )
            .join(JobResult, JobResult.id == DocumentChunk.job_result_id)
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
            .order_by(
                DocumentChunk.sort_order.asc(),
                DocumentChunk.created_at.asc(),
                DocumentChunk.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        if chunk_type is not None:
            stmt = stmt.where(func.lower(DocumentChunk.chunk_type) == chunk_type)

        result = await db.execute(stmt)
        return cast(Sequence[DocumentChunkRow], result.all())

    async def get_current_document_chunk(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
        document_chunk_id: str,
    ) -> DocumentChunkRow | None:
        stmt = (
            select(DocumentChunk, DocumentSection, JobResult)
            .outerjoin(
                DocumentSection,
                DocumentSection.section_id == DocumentChunk.section_id,
            )
            .join(JobResult, JobResult.id == DocumentChunk.job_result_id)
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
            .where(DocumentChunk.id == document_chunk_id)
            .limit(1)
        )

        result = await db.execute(stmt)
        row = result.first()
        return cast(DocumentChunkRow | None, row)
