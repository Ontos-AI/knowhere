"""Application workflow for document lifecycle routes."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from app.repositories.document_repository import DocumentRepository
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import DocumentChunk, DocumentSection
from shared.services.retrieval.cache_service import (
    invalidate_retrieval_cache_namespaces,
)
from shared.services.retrieval.graph.service import DocumentGraphService, GraphScope


def document_payload(document) -> dict[str, Any]:
    return {
        "document_id": document.document_id,
        "namespace": document.namespace,
        "status": document.status,
        "current_job_result_id": document.current_job_result_id,
        "source_file_name": document.source_file_name,
        "created_at": document.created_at.isoformat() if document.created_at else None,
        "updated_at": document.updated_at.isoformat() if document.updated_at else None,
        "archived_at": (
            document.archived_at.isoformat() if document.archived_at else None
        ),
    }


def _datetime_payload(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class DocumentService:
    def __init__(
        self,
        *,
        repository: DocumentRepository | None = None,
        graph_service: DocumentGraphService | None = None,
    ) -> None:
        self._repository = repository or DocumentRepository()
        self._graph_service = graph_service or DocumentGraphService()

    async def list_documents(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
    ) -> list[dict[str, Any]]:
        documents = await self._repository.list_by_user_namespace(
            db,
            user_id=user_id,
            namespace=namespace,
        )
        return [document_payload(document) for document in documents]

    async def list_document_chunks(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        document_id: str,
        page: int,
        page_size: int,
        chunk_type: str | None,
    ) -> dict[str, Any] | None:
        document = await self._repository.get_document(
            db,
            user_id=user_id,
            document_id=document_id,
        )
        if document is None:
            return None

        job_result_id = document.current_job_result_id
        if not job_result_id:
            return {
                "document_id": document.document_id,
                "namespace": document.namespace,
                "job_result_id": None,
                "job_id": None,
                "chunks": [],
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": 0,
                    "total_pages": 0,
                },
            }

        normalized_chunk_type = _normalize_chunk_type_filter(chunk_type)
        total = await self._repository.count_current_document_chunks(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
            chunk_type=normalized_chunk_type,
        )
        rows = await self._repository.list_current_document_chunks(
            db,
            document_id=document_id,
            job_result_id=job_result_id,
            limit=page_size,
            offset=(page - 1) * page_size,
            chunk_type=normalized_chunk_type,
        )
        chunks = [
            self._chunk_payload(
                chunk=chunk,
                section=section,
            )
            for chunk, section, job_result in rows
        ]
        job_id = rows[0][2].job_id if rows else None

        return {
            "document_id": document.document_id,
            "namespace": document.namespace,
            "job_result_id": job_result_id,
            "job_id": job_id,
            "chunks": chunks,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": math.ceil(total / page_size) if total else 0,
            },
        }

    async def get_document_chunk(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        document_id: str,
        document_chunk_id: str,
    ) -> dict[str, Any] | None:
        document = await self._repository.get_document(
            db,
            user_id=user_id,
            document_id=document_id,
        )
        if document is None or not document.current_job_result_id:
            return None

        row = await self._repository.get_current_document_chunk(
            db,
            document_id=document_id,
            job_result_id=document.current_job_result_id,
            document_chunk_id=document_chunk_id,
        )
        if row is None:
            return None

        chunk, section, job_result = row
        return {
            "document_id": document.document_id,
            "namespace": document.namespace,
            "job_result_id": document.current_job_result_id,
            "job_id": job_result.job_id,
            "chunk": self._chunk_payload(
                chunk=chunk,
                section=section,
            ),
        }

    async def get_document(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        document_id: str,
    ) -> dict[str, Any] | None:
        document = await self._repository.get_document(
            db,
            user_id=user_id,
            document_id=document_id,
        )
        if document is None:
            return None
        return document_payload(document)

    def _chunk_payload(
        self,
        *,
        chunk: DocumentChunk,
        section: DocumentSection | None,
    ) -> dict[str, Any]:
        chunk_type = _normalize_chunk_type(chunk.chunk_type)
        file_path = chunk.file_path
        return {
            "id": chunk.id,
            "chunk_id": chunk.chunk_id,
            "chunk_type": chunk_type,
            "content": chunk.content,
            "section_id": chunk.section_id,
            "section_path": section.section_path if section else None,
            "source_chunk_path": chunk.source_chunk_path,
            "file_path": file_path,
            "sort_order": chunk.sort_order,
            "metadata": chunk.chunk_metadata,
            "created_at": _datetime_payload(chunk.created_at),
        }

    async def archive_document(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        document_id: str,
    ) -> dict[str, Any] | None:
        document = await self._repository.get_document(
            db,
            user_id=user_id,
            document_id=document_id,
        )
        if document is None:
            return None

        if document.status == "archived":
            return document_payload(document)

        previous_namespace = document.namespace
        await self._repository.archive_document(db, document=document)
        await db.run_sync(
            lambda sync_db: self._graph_service.remove_document_graph(
                sync_db,
                scope=GraphScope(user_id=user_id, namespace=document.namespace),
                document_id=document_id,
            )
        )
        await db.commit()
        try:
            await invalidate_retrieval_cache_namespaces(
                user_id=user_id,
                namespaces=[previous_namespace],
            )
        except Exception as e:
            logger.warning(
                f"Cache invalidation failed after archiving document {document_id}: {e}"
            )
        return document_payload(document)


def _normalize_chunk_type(raw: str | None) -> str:
    return str(raw or "").strip().split("\n", 1)[0].lower()


def _normalize_chunk_type_filter(raw: str | None) -> str | None:
    if raw is None:
        return None
    normalized = _normalize_chunk_type(raw)
    return normalized or None
