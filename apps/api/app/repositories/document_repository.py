"""
Document data access for retrieval document lifecycle flows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, cast

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult

DocumentChunkRow = tuple[DocumentChunk, DocumentSection | None, JobResult]
DocumentChunkInspectionRow = tuple[DocumentChunk, DocumentSection | None, JobResult, int]
DocumentChunkScopeBounds = tuple[int, int]


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

    async def get_active_document_in_namespace(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        user_id: str,
        namespace: str,
    ) -> Document | None:
        result = await db.execute(
            select(Document)
            .where(Document.document_id == document_id)
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(Document.status == "active")
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

    async def list_current_document_sections(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
    ) -> Sequence[DocumentSection]:
        result = await db.execute(
            select(DocumentSection)
            .where(DocumentSection.document_id == document_id)
            .where(DocumentSection.job_result_id == job_result_id)
            .order_by(
                DocumentSection.sort_order.asc(),
                DocumentSection.created_at.asc(),
                DocumentSection.section_id.asc(),
            )
        )
        return result.scalars().all()

    async def list_current_document_chunks_for_inspection(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
        limit: int,
        offset: int = 0,
        chunk_type: str | None = None,
        minimum_ordinal: int | None = None,
        maximum_ordinal: int | None = None,
        document_chunk_id: str | None = None,
        chunk_id: str | None = None,
        section_path: str | None = None,
        section_path_prefix: str | None = None,
    ) -> Sequence[DocumentChunkInspectionRow]:
        ordinal_subquery = _create_chunk_ordinal_subquery(
            document_id=document_id,
            job_result_id=job_result_id,
        )
        stmt = (
            select(
                DocumentChunk,
                DocumentSection,
                JobResult,
                ordinal_subquery.c.ordinal,
            )
            .join(
                ordinal_subquery,
                ordinal_subquery.c.document_chunk_id == DocumentChunk.id,
            )
            .outerjoin(
                DocumentSection,
                DocumentSection.section_id == DocumentChunk.section_id,
            )
            .join(JobResult, JobResult.id == DocumentChunk.job_result_id)
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
            .order_by(ordinal_subquery.c.ordinal.asc())
            .limit(limit)
            .offset(offset)
        )
        if chunk_type is not None:
            stmt = stmt.where(func.lower(DocumentChunk.chunk_type) == chunk_type)
        if minimum_ordinal is not None:
            stmt = stmt.where(ordinal_subquery.c.ordinal >= minimum_ordinal)
        if maximum_ordinal is not None:
            stmt = stmt.where(ordinal_subquery.c.ordinal <= maximum_ordinal)
        if document_chunk_id is not None:
            stmt = stmt.where(DocumentChunk.id == document_chunk_id)
        if chunk_id is not None:
            stmt = stmt.where(DocumentChunk.chunk_id == chunk_id)
        stmt = _apply_section_filters(
            stmt,
            section_path=section_path,
            section_path_prefix=section_path_prefix,
        )

        result = await db.execute(stmt)
        return cast(Sequence[DocumentChunkInspectionRow], result.all())

    async def get_current_document_chunk_scope_bounds(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        job_result_id: str,
        section_path: str | None = None,
    ) -> DocumentChunkScopeBounds | None:
        ordinal_subquery = _create_chunk_ordinal_subquery(
            document_id=document_id,
            job_result_id=job_result_id,
        )
        stmt = (
            select(
                func.min(ordinal_subquery.c.ordinal),
                func.max(ordinal_subquery.c.ordinal),
            )
            .select_from(DocumentChunk)
            .join(
                ordinal_subquery,
                ordinal_subquery.c.document_chunk_id == DocumentChunk.id,
            )
            .outerjoin(
                DocumentSection,
                DocumentSection.section_id == DocumentChunk.section_id,
            )
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
        )
        stmt = _apply_section_filters(
            stmt,
            section_path=section_path,
            section_path_prefix=None,
        )

        result = await db.execute(stmt)
        minimum_ordinal, maximum_ordinal = result.one()
        if minimum_ordinal is None or maximum_ordinal is None:
            return None
        return int(minimum_ordinal), int(maximum_ordinal)

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


def _create_chunk_ordinal_subquery(
    *,
    document_id: str,
    job_result_id: str,
):
    return (
        select(
            DocumentChunk.id.label("document_chunk_id"),
            func.row_number()
            .over(
                order_by=(
                    DocumentChunk.sort_order.asc(),
                    DocumentChunk.created_at.asc(),
                    DocumentChunk.id.asc(),
                )
            )
            .label("ordinal"),
        )
        .where(DocumentChunk.document_id == document_id)
        .where(DocumentChunk.job_result_id == job_result_id)
        .subquery()
    )


def _apply_section_filters(
    stmt,
    *,
    section_path: str | None,
    section_path_prefix: str | None,
):
    if section_path is not None:
        if section_path == "(root)":
            return stmt.where(DocumentChunk.section_id.is_(None))
        return stmt.where(DocumentSection.section_path == section_path)
    if section_path_prefix is not None:
        if section_path_prefix == "(root)":
            return stmt.where(DocumentChunk.section_id.is_(None))
        escaped_prefix = _escape_like_pattern(section_path_prefix)
        return stmt.where(
            or_(
                DocumentSection.section_path == section_path_prefix,
                DocumentSection.section_path.like(
                    f"{escaped_prefix} /%",
                    escape="\\",
                ),
            )
        )
    return stmt


def _escape_like_pattern(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
