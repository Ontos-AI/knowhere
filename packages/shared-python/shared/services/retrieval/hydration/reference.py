from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult
from shared.services.retrieval.hydration.row_utils import (
    ReferenceLookupKey,
    build_reference_lookup_key,
)


async def hydrate_referenced_chunk_rows(
    *,
    db: AsyncSession | None,
    user_id: str,
    namespace: str,
    refs: list[dict[str, Any]],
    score_by_chunk_id: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    if db is None or not refs:
        return []

    ref_keys = [
        build_reference_lookup_key(
            document_id=ref.get('document_id'),
            chunk_id=ref.get('chunk_id'),
            section_path=ref.get('section_path'),
            file_path=ref.get('file_path'),
        )
        for ref in refs
    ]
    ref_keys = [key for key in ref_keys if key[0] and key[1]]
    if not ref_keys:
        return []

    document_ids = sorted({document_id for document_id, _, _, _ in ref_keys})
    chunk_ids = sorted({chunk_id for _, chunk_id, _, _ in ref_keys})
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
        .where(Document.document_id.in_(document_ids))
        .where(DocumentChunk.chunk_id.in_(chunk_ids))
        .order_by(DocumentChunk.sort_order)
    )
    result = await db.execute(stmt)

    rows_by_key: dict[ReferenceLookupKey, dict[str, Any]] = {}
    rows_by_base_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for document, chunk, section, job_result in result.all():
        row = {
            'document_id': document.document_id,
            'chunk_id': chunk.chunk_id,
            'section_id': chunk.section_id,
            'section_path': section.section_path if section else None,
            'source_file_name': document.source_file_name,
            'chunk_type': chunk.chunk_type,
            'content': chunk.content,
            # Use the caller-supplied score when available (e.g. discovery RRF or
            # KG confidence). None signals "no score known" so consumers can
            # distinguish unscored chunks from genuinely high-scoring ones.
            'score': (
                score_by_chunk_id.get(chunk.chunk_id)
                if score_by_chunk_id is not None
                else None
            ),
            'file_path': chunk.file_path,
            'chunk_metadata': chunk.chunk_metadata or {},
            'job_result_id': chunk.job_result_id,
            'job_id': job_result.job_id if job_result else None,
            'source_chunk_path': chunk.source_chunk_path,
            'sort_order': chunk.sort_order,
        }
        key = build_reference_lookup_key(
            document_id=row['document_id'],
            chunk_id=row['chunk_id'],
            section_path=row['section_path'],
            file_path=row['file_path'],
        )
        rows_by_key[key] = row
        rows_by_base_key.setdefault((key[0], key[1]), []).append(row)

    rows: list[dict[str, Any]] = []
    seen_keys: set[ReferenceLookupKey] = set()
    for key in ref_keys:
        row = rows_by_key.get(key)
        if row is None:
            candidates = rows_by_base_key.get((key[0], key[1]), [])
            row = next(
                (
                    candidate
                    for candidate in candidates
                    if key[2]
                    and str(candidate.get('section_path') or '').strip() == key[2]
                ),
                None,
            )
            if row is None:
                row = next(
                    (
                        candidate
                        for candidate in candidates
                        if build_reference_lookup_key(
                            document_id=candidate.get('document_id'),
                            chunk_id=candidate.get('chunk_id'),
                            section_path=candidate.get('section_path'),
                            file_path=candidate.get('file_path'),
                        )
                        not in seen_keys
                    ),
                    None,
                )
        if row is not None:
            row_key = build_reference_lookup_key(
                document_id=row.get('document_id'),
                chunk_id=row.get('chunk_id'),
                section_path=row.get('section_path'),
                file_path=row.get('file_path'),
            )
            if row_key in seen_keys:
                continue
            seen_keys.add(row_key)
            rows.append(row)
    return rows
