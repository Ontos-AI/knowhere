"""
Document data access for retrieval document lifecycle flows.
"""

from __future__ import annotations

from shared.services.retrieval.corpus_storage import CorpusStorage
from app.services.demo.revision_reader import resolve_demo_revision

from datetime import datetime, timezone
from typing import Sequence, cast

from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult

DocumentChunkRow = tuple[DocumentChunk, DocumentSection | None, JobResult]
DocumentJobRevisionRow = tuple[Document, JobResult, Job]


class DocumentRepository:
    async def list_by_user_namespace(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        limit: int,
        offset: int,
    ) -> Sequence[Document]:
        corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
        result = await db.execute(
            select(corpusStorage.Document)
            .where(corpusStorage.Document.user_id == corpusStorage.resolve_owner(user_id))
            .where(corpusStorage.Document.namespace == namespace)
            .where(corpusStorage.Document.status != "archived")
            .order_by(corpusStorage.Document.updated_at.desc(), corpusStorage.Document.document_id.asc())
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all()

    async def count_by_user_namespace(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
    ) -> int:
        corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
        result = await db.execute(
            select(func.count(corpusStorage.Document.document_id))
            .where(corpusStorage.Document.user_id == corpusStorage.resolve_owner(user_id))
            .where(corpusStorage.Document.namespace == namespace)
            .where(corpusStorage.Document.status != "archived")
        )
        return int(result.scalar_one())

    async def get_document(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        user_id: str,
    ) -> Document | None:
        corpusStorage: CorpusStorage = CorpusStorage.resolve_document(document_id)
        result = await db.execute(
            select(corpusStorage.Document)
            .where(corpusStorage.Document.document_id == document_id)
            .where(corpusStorage.Document.user_id == corpusStorage.resolve_owner(user_id))
            .where(corpusStorage.Document.status != "archived" if corpusStorage.is_demo else true())
        )
        return result.scalar_one_or_none()

    async def get_current_document_job_revision(
        self,
        db: AsyncSession,
        *,
        document_id: str,
        user_id: str,
        job_result_id: str | None = None,
    ) -> DocumentJobRevisionRow | None:
        corpusStorage: CorpusStorage = CorpusStorage.resolve_document(document_id)
        if corpusStorage.is_demo:
            document, result = await resolve_demo_revision(db, document_id=document_id, job_result_id=job_result_id)
            job = (await db.execute(select(Job).where(Job.job_id == result.job_id))).scalar_one()
            return document, result, job
        stmt = (
            select(corpusStorage.Document, JobResult, Job)
            .join(JobResult, JobResult.id == (job_result_id or corpusStorage.Document.current_job_result_id))
            .join(Job, Job.job_id == JobResult.job_id)
            .where(corpusStorage.Document.document_id == document_id)
            .where(corpusStorage.Document.user_id == corpusStorage.resolve_owner(user_id))
            .where(JobResult.document_id == corpusStorage.Document.document_id)
            .where(Job.user_id == user_id)
            .where(corpusStorage.Document.status != "archived")
            .limit(1)
        )

        result = await db.execute(stmt)
        row = result.first()
        return cast(DocumentJobRevisionRow | None, row)

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
        corpusStorage: CorpusStorage = CorpusStorage.resolve_document(document_id)
        stmt = (
            select(func.count(corpusStorage.DocumentChunk.id))
            .where(corpusStorage.DocumentChunk.document_id == document_id)
            .where(corpusStorage.DocumentChunk.job_result_id == job_result_id)
        )
        if chunk_type is not None:
            stmt = stmt.where(func.lower(corpusStorage.DocumentChunk.chunk_type) == chunk_type)

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
        corpusStorage: CorpusStorage = CorpusStorage.resolve_document(document_id)
        stmt = (
            select(corpusStorage.DocumentChunk, corpusStorage.DocumentSection, JobResult).options(noload(JobResult.chunks), noload(JobResult.job))
            .outerjoin(
                corpusStorage.DocumentSection,
                corpusStorage.DocumentSection.section_id == corpusStorage.DocumentChunk.section_id,
            )
            .join(JobResult, JobResult.id == corpusStorage.DocumentChunk.job_result_id)
            .where(corpusStorage.DocumentChunk.document_id == document_id)
            .where(corpusStorage.DocumentChunk.job_result_id == job_result_id)
            .order_by(
                corpusStorage.DocumentChunk.sort_order.asc(),
                corpusStorage.DocumentChunk.created_at.asc(),
                corpusStorage.DocumentChunk.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        if chunk_type is not None:
            stmt = stmt.where(func.lower(corpusStorage.DocumentChunk.chunk_type) == chunk_type)

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
        corpusStorage: CorpusStorage = CorpusStorage.resolve_document(document_id)
        stmt = (
            select(corpusStorage.DocumentChunk, corpusStorage.DocumentSection, JobResult).options(noload(JobResult.chunks), noload(JobResult.job))
            .outerjoin(
                corpusStorage.DocumentSection,
                corpusStorage.DocumentSection.section_id == corpusStorage.DocumentChunk.section_id,
            )
            .join(JobResult, JobResult.id == corpusStorage.DocumentChunk.job_result_id)
            .where(corpusStorage.DocumentChunk.document_id == document_id)
            .where(corpusStorage.DocumentChunk.job_result_id == job_result_id)
            .where(corpusStorage.DocumentChunk.id == document_chunk_id)
            .limit(1)
        )

        result = await db.execute(stmt)
        row = result.first()
        return cast(DocumentChunkRow | None, row)
