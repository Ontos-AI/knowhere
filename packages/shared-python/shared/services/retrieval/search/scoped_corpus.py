from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import and_, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError

from shared.models.database.document import (
    Document,
    DocumentChunk,
    DocumentSection,
    RetrievalServingRevisionManifest,
)
from shared.models.database.job_result import JobResult
from shared.services.retrieval.search.section_filters import is_excluded_section
from shared.services.retrieval.serving_manifest import decode_serving_manifest
from shared.services.retrieval.manifest_cache import cache_manifest_payloads


async def count_manifest_chunks(
    db: AsyncSession,
    *,
    revision_pins: Mapping[str, str],
) -> int | None:
    """Count chunks exactly from complete pinned manifests when available."""
    if not revision_pins:
        return None
    statement = select(
        RetrievalServingRevisionManifest.document_id,
        RetrievalServingRevisionManifest.job_result_id,
        RetrievalServingRevisionManifest.payload_zlib,
        RetrievalServingRevisionManifest.checksum,
        RetrievalServingRevisionManifest.format_version,
    ).where(
        tuple_(
            RetrievalServingRevisionManifest.document_id,
            RetrievalServingRevisionManifest.job_result_id,
        ).in_(list(revision_pins.items()))
    )
    try:
        rows = (await db.execute(statement)).all()
        if len(rows) != len(revision_pins):
            return None
        total = 0
        decoded_payloads: dict[tuple[str, str], dict[str, Any]] = {}
        for document_id, job_result_id, payload_zlib, checksum, format_version in rows:
            payload = decode_serving_manifest(
                bytes(payload_zlib),
                checksum=str(checksum),
                format_version=int(format_version),
            )
            chunks = payload.get("chunks")
            if not isinstance(chunks, list):
                return None
            total += len(chunks)
            decoded_payloads[(str(document_id), str(job_result_id))] = payload
        cache_manifest_payloads(
            db,
            revisions=revision_pins,
            payloads=decoded_payloads,
        )
        return total
    except SQLAlchemyError:
        await db.rollback()
        return None
    except (TypeError, ValueError, KeyError):
        return None


async def count_scoped_chunks(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    exclude_document_ids: list[str],
    allowed_chunk_types: set[str] | None,
    revision_pins: Mapping[str, str] | None = None,
    max_count: int | None = None,
) -> int:
    if revision_pins is None:
        stmt = (
            select(DocumentChunk.id)
            .join(
                Document,
                (Document.document_id == DocumentChunk.document_id)
                & (Document.current_job_result_id == DocumentChunk.job_result_id),
            )
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(Document.status == 'active')
        )
    else:
        stmt = (
            select(DocumentChunk.id)
            .join(Document, Document.document_id == DocumentChunk.document_id)
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(
                tuple_(DocumentChunk.document_id, DocumentChunk.job_result_id).in_(
                    list(revision_pins.items())
                )
            )
        )
    if exclude_document_ids:
        stmt = stmt.where(Document.document_id.notin_(list(exclude_document_ids)))
    if allowed_chunk_types is not None:
        stmt = stmt.where(func.lower(DocumentChunk.chunk_type).in_(list(allowed_chunk_types)))

    if max_count is not None:
        stmt = stmt.limit(max_count)

    result = await db.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    )
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
    revision_pins: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    if revision_pins is None:
        chunk_join = (
            (DocumentChunk.document_id == Document.document_id)
            & (DocumentChunk.job_result_id == Document.current_job_result_id)
        )
    else:
        chunk_join = and_(
            DocumentChunk.document_id == Document.document_id,
            tuple_(DocumentChunk.document_id, DocumentChunk.job_result_id).in_(
                list(revision_pins.items())
            ),
        )
    stmt = (
        select(Document, DocumentChunk, DocumentSection, JobResult)
        .join(DocumentChunk, chunk_join)
        .outerjoin(DocumentSection, DocumentSection.section_id == DocumentChunk.section_id)
        .join(JobResult, JobResult.id == DocumentChunk.job_result_id)
        .where(Document.user_id == user_id)
        .where(Document.namespace == namespace)
        .order_by(DocumentChunk.sort_order)
    )
    if revision_pins is None:
        stmt = stmt.where(Document.status == 'active')
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
            'source_chunk_path': chunk.source_chunk_path,
            'file_path': chunk.file_path,
            'chunk_metadata': chunk.chunk_metadata or {},
            'job_result_id': chunk.job_result_id,
            'job_id': job_result.job_id if job_result else None,
            'sort_order': chunk.sort_order,
        })
    return rows
