"""Application workflow for document lifecycle routes."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from app.repositories.document_repository import DocumentRepository
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import DocumentChunk, DocumentSection
from shared.services.retrieval.cache_service import (
    invalidate_retrieval_cache_namespaces,
)
from shared.services.retrieval.graph.service import DocumentGraphService, GraphScope
from shared.services.storage.result_storage import ResultStorage, get_result_storage

_DOCUMENT_CHUNK_ASSET_URL_EXPIRES_SECONDS = 7 * 24 * 60 * 60
_MEDIA_CHUNK_TYPES = frozenset({"image", "table"})
_PAGE_CITATION_SOURCE_EXPIRES_SECONDS = 60 * 60
_PAGE_CITATION_SOURCE_FILE_NAME = "source.pdf"
_PAGE_CITATION_SOURCE_VARIANT = "normalized_pdf"
_PAGE_MEMORY_PARSE_TRACK = "page_memory"


def _datetime_payload(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _document_chunk_asset_url(
    *,
    chunk_type: str,
    job_id: str | None,
    file_path: str | None,
    include_asset_urls: bool,
    result_storage: ResultStorage | None,
) -> str | None:
    if (
        not include_asset_urls
        or chunk_type not in _MEDIA_CHUNK_TYPES
        or not job_id
        or not file_path
        or result_storage is None
    ):
        return None

    try:
        return result_storage.generate_artifact_url(
            job_id=job_id,
            artifact_ref=file_path,
            expires_in=_DOCUMENT_CHUNK_ASSET_URL_EXPIRES_SECONDS,
        )
    except Exception as exc:
        logger.warning(f"Failed to generate document chunk asset URL (ignored): {exc}")
        return None


def _document_page_assets(
    *,
    metadata: dict[str, Any] | None,
    job_id: str | None,
    include_asset_urls: bool,
    result_storage: ResultStorage | None,
) -> list[dict[str, Any]]:
    if not isinstance(metadata, dict):
        return []
    raw_assets = metadata.get("page_assets")
    if not isinstance(raw_assets, list):
        return []

    page_assets: list[dict[str, Any]] = []
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            continue
        asset = _normalize_page_asset(raw_asset)
        if asset is None:
            continue
        if include_asset_urls and job_id and result_storage is not None:
            asset_url = _page_asset_url(
                job_id=job_id,
                artifact_ref=asset["artifact_ref"],
                result_storage=result_storage,
            )
            if asset_url:
                asset["asset_url"] = asset_url
        page_assets.append(asset)
    return page_assets


def _normalize_page_asset(raw_asset: dict[str, Any]) -> dict[str, Any] | None:
    page_num = _positive_int(raw_asset.get("page_num"))
    artifact_ref = str(raw_asset.get("artifact_ref") or "").strip()
    content_type = str(raw_asset.get("content_type") or "").strip()
    source = str(raw_asset.get("source") or "").strip()
    if page_num is None or not artifact_ref or not content_type or not source:
        return None

    asset: dict[str, Any] = {
        "page_num": page_num,
        "artifact_ref": artifact_ref,
        "content_type": content_type,
        "source": source,
    }
    if (asset_url := str(raw_asset.get("asset_url") or "").strip()):
        asset["asset_url"] = asset_url
    if (width := _positive_int(raw_asset.get("width"))) is not None:
        asset["width"] = width
    if (height := _positive_int(raw_asset.get("height"))) is not None:
        asset["height"] = height
    return asset


def _page_asset_url(
    *,
    job_id: str,
    artifact_ref: str,
    result_storage: ResultStorage,
) -> str | None:
    normalized_ref = result_storage.normalize_artifact_ref(artifact_ref)
    if not normalized_ref or not normalized_ref.startswith("page_citation_assets/"):
        return None
    try:
        return result_storage.generate_artifact_url(
            job_id=job_id,
            artifact_ref=normalized_ref,
            expires_in=_DOCUMENT_CHUNK_ASSET_URL_EXPIRES_SECONDS,
        )
    except Exception as exc:
        logger.warning(f"Failed to generate page citation asset URL (ignored): {exc}")
        return None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def document_payload(document) -> dict[str, Any]:
    return {
        "document_id": document.document_id,
        "namespace": document.namespace,
        "status": document.status,
        "current_job_result_id": document.current_job_result_id,
        "source_file_name": document.source_file_name,
        "document_metadata": document.document_metadata or {},
        "created_at": document.created_at.isoformat() if document.created_at else None,
        "updated_at": document.updated_at.isoformat() if document.updated_at else None,
        "archived_at": (
            document.archived_at.isoformat() if document.archived_at else None
        ),
    }


class DocumentService:
    def __init__(
        self,
        *,
        repository: DocumentRepository | None = None,
        graph_service: DocumentGraphService | None = None,
        result_storage: ResultStorage | None = None,
    ) -> None:
        self._repository = repository or DocumentRepository()
        self._graph_service = graph_service or DocumentGraphService()
        self._result_storage = result_storage

    async def list_documents(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        total = await self._repository.count_by_user_namespace(
            db,
            user_id=user_id,
            namespace=namespace,
        )
        documents = await self._repository.list_by_user_namespace(
            db,
            user_id=user_id,
            namespace=namespace,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        return {
            "namespace": namespace,
            "documents": [document_payload(document) for document in documents],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": math.ceil(total / page_size) if total else 0,
            },
        }

    async def list_namespaces(
        self,
        db: AsyncSession,
        *,
        user_id: str,
    ) -> dict[str, Any]:
        rows = await self._repository.list_namespace_counts_for_user(
            db,
            user_id=user_id,
        )
        namespaces = [
            {"namespace": namespace, "document_count": count}
            for namespace, count in rows
        ]
        return {"namespaces": namespaces}

    async def list_document_chunks(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        document_id: str,
        page: int,
        page_size: int,
        chunk_type: str | None,
        include_asset_urls: bool,
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
        result_storage = get_result_storage() if include_asset_urls else None
        chunks = [
            self._chunk_payload(
                chunk=chunk,
                section=section,
                job_id=job_result.job_id,
                include_asset_urls=include_asset_urls,
                result_storage=result_storage,
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
        include_asset_urls: bool,
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
        result_storage = get_result_storage() if include_asset_urls else None
        return {
            "document_id": document.document_id,
            "namespace": document.namespace,
            "job_result_id": document.current_job_result_id,
            "job_id": job_result.job_id,
            "chunk": self._chunk_payload(
                chunk=chunk,
                section=section,
                job_id=job_result.job_id,
                include_asset_urls=include_asset_urls,
                result_storage=result_storage,
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

    async def get_document_page_citation_source(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        document_id: str,
    ) -> dict[str, Any] | None:
        row = await self._repository.get_current_document_job_revision(
            db,
            user_id=user_id,
            document_id=document_id,
        )
        if row is None:
            return None

        document, job_result, job = row
        if document.parse_track != _PAGE_MEMORY_PARSE_TRACK:
            return None

        result_storage = self._result_storage or get_result_storage()
        if not result_storage.verify_raw_exists(
            job_id=job_result.job_id,
            relative_path=_PAGE_CITATION_SOURCE_FILE_NAME,
        ):
            return None

        source_url = result_storage.generate_raw_file_url(
            job_id=job_result.job_id,
            relative_path=_PAGE_CITATION_SOURCE_FILE_NAME,
            expires_in=_PAGE_CITATION_SOURCE_EXPIRES_SECONDS,
        )
        if not source_url:
            return None

        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=_PAGE_CITATION_SOURCE_EXPIRES_SECONDS,
        )
        return {
            "document_id": document.document_id,
            "namespace": document.namespace,
            "job_id": job.job_id,
            "job_result_id": job_result.id,
            "variant": _PAGE_CITATION_SOURCE_VARIANT,
            "file_name": _PAGE_CITATION_SOURCE_FILE_NAME,
            "content_type": "application/pdf",
            "url": source_url,
            "expires_at": expires_at.isoformat(),
        }

    def _chunk_payload(
        self,
        *,
        chunk: DocumentChunk,
        section: DocumentSection | None,
        job_id: str | None,
        include_asset_urls: bool,
        result_storage: ResultStorage | None,
    ) -> dict[str, Any]:
        chunk_type = _normalize_chunk_type(chunk.chunk_type)
        file_path = chunk.file_path
        raw_metadata = chunk.chunk_metadata or {}
        page_assets = _document_page_assets(
            metadata=raw_metadata,
            job_id=job_id,
            include_asset_urls=include_asset_urls,
            result_storage=result_storage,
        )
        metadata = dict(raw_metadata)
        if include_asset_urls and page_assets:
            metadata["page_assets"] = page_assets
        payload = {
            "id": chunk.id,
            "chunk_id": chunk.chunk_id,
            "chunk_type": chunk_type,
            "content": chunk.content,
            "section_id": chunk.section_id,
            "section_path": section.section_path if section else None,
            "source_chunk_path": chunk.source_chunk_path,
            "file_path": file_path,
            "sort_order": chunk.sort_order,
            "metadata": metadata,
            "asset_url": _document_chunk_asset_url(
                chunk_type=chunk_type,
                job_id=job_id,
                file_path=file_path,
                include_asset_urls=include_asset_urls,
                result_storage=result_storage,
            ),
            "created_at": _datetime_payload(chunk.created_at),
        }
        return payload

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
