from __future__ import annotations

import re
from typing import Any

from loguru import logger
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult
from shared.services.retrieval.graph_service import is_excluded_section
from shared.services.retrieval.lexical_text import normalize_section_path
from shared.services.retrieval.scoring import get_row_path

MEDIA_CHUNK_TYPES = {'image', 'table'}
PUBLIC_RESULT_FIELDS = {
    'chunk_type', 'content', 'score', 'asset_url',
}
PUBLIC_SOURCE_FIELDS = {
    'document_id', 'source_file_name', 'section_path',
}

ReferenceLookupKey = tuple[str, str, str, str]

_PATH_REF_RE = re.compile(r'\[(?:images|tables)/[^\]\n]+\]')


def clean_content(content: str) -> str:
    return _PATH_REF_RE.sub('', content).strip()


def normalize_chunk_type(raw: object) -> str:
    return str(raw or '').strip().split('\n', 1)[0].lower()


def is_media_chunk(row: dict[str, Any]) -> bool:
    return normalize_chunk_type(row.get('chunk_type')) in MEDIA_CHUNK_TYPES


def build_reference_lookup_key(
    *,
    document_id: object,
    chunk_id: object,
    section_path: object = '',
    file_path: object = '',
) -> ReferenceLookupKey:
    return (
        str(document_id or '').strip(),
        str(chunk_id or '').strip(),
        str(section_path or '').strip(),
        str(file_path or '').strip(),
    )


def filter_excluded_rows(
    rows: list[dict[str, Any]],
    *,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    excluded_documents = set(exclude_document_ids)
    for row in rows:
        document_id = row.get('document_id')
        if document_id in excluded_documents:
            continue
        if is_excluded_section(
            document_id=document_id,
            section_path=row.get('section_path'),
            exclude_sections=exclude_sections,
        ):
            continue
        filtered.append(row)
    return filtered


def iter_connected_target_ids(row: dict[str, Any]) -> list[str]:
    metadata = row.get('chunk_metadata') or {}
    if not isinstance(metadata, dict):
        return []

    target_ids: list[str] = []
    for item in metadata.get('connect_to') or []:
        if not isinstance(item, dict):
            continue
        target_id = str(item.get('target') or '').strip()
        if target_id:
            target_ids.append(target_id)
    return target_ids


async def hydrate_connected_target_rows(
    *,
    db: AsyncSession | None,
    rows: list[dict[str, Any]],
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
) -> list[dict[str, Any]]:
    if db is None:
        return []

    existing_chunk_ids = {
        str(row.get('chunk_id') or '').strip()
        for row in rows
        if row.get('chunk_id')
    }
    target_ids_by_revision: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        if normalize_chunk_type(row.get('chunk_type')) != 'text':
            continue
        document_id = str(row.get('document_id') or '').strip()
        job_result_id = str(row.get('job_result_id') or '').strip()
        if not document_id or not job_result_id:
            continue
        for target_id in iter_connected_target_ids(row):
            if target_id in existing_chunk_ids:
                continue
            target_ids_by_revision.setdefault((document_id, job_result_id), set()).add(target_id)

    if not target_ids_by_revision:
        return []

    revision_filters = [
        and_(
            DocumentChunk.document_id == document_id,
            DocumentChunk.job_result_id == job_result_id,
            DocumentChunk.chunk_id.in_(sorted(target_ids)),
        )
        for (document_id, job_result_id), target_ids in target_ids_by_revision.items()
        if target_ids
    ]
    if not revision_filters:
        return []

    stmt = (
        select(Document, DocumentChunk, DocumentSection, JobResult)
        .join(DocumentChunk, DocumentChunk.document_id == Document.document_id)
        .outerjoin(DocumentSection, DocumentSection.section_id == DocumentChunk.section_id)
        .join(JobResult, JobResult.id == DocumentChunk.job_result_id)
        .where(or_(*revision_filters))
        .order_by(DocumentChunk.sort_order)
    )
    result = await db.execute(stmt)

    hydrated_rows: list[dict[str, Any]] = []
    for document, chunk, section, job_result in result.all():
        section_path = section.section_path if section else None
        hydrated_rows.append(
            {
                'document_id': document.document_id,
                'chunk_id': chunk.chunk_id,
                'section_id': chunk.section_id,
                'section_path': section_path,
                'source_file_name': document.source_file_name,
                'chunk_type': chunk.chunk_type,
                'content': chunk.content,
                'score': 0.0,
                'file_path': chunk.file_path,
                'chunk_metadata': chunk.chunk_metadata or {},
                'job_result_id': chunk.job_result_id,
                'job_id': job_result.job_id if job_result else None,
                'sort_order': chunk.sort_order,
            }
        )

    return filter_excluded_rows(
        hydrated_rows,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
    )


async def hydrate_referenced_chunk_rows(
    *,
    db: AsyncSession | None,
    user_id: str,
    namespace: str,
    refs: list[dict[str, Any]],
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
            'score': 1.0,
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
                    candidate for candidate in candidates
                    if key[2] and str(candidate.get('section_path') or '').strip() == key[2]
                ),
                None,
            )
            if row is None:
                row = next(
                    (
                        candidate for candidate in candidates
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


async def assemble_retrieval_results(
    *,
    db: AsyncSession | None = None,
    rows: list[dict[str, Any]],
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    allowed_chunk_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    filtered_rows = filter_excluded_rows(
        rows,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
    )
    if allowed_chunk_types is not None:
        filtered_rows = [
            row for row in filtered_rows
            if normalize_chunk_type(row.get('chunk_type')) in allowed_chunk_types
        ]
    hydrated_rows = await hydrate_connected_target_rows(
        db=db,
        rows=filtered_rows,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
    )
    rows_by_chunk_id = {
        str(row.get('chunk_id') or ''): row
        for row in [*filtered_rows, *hydrated_rows]
        if row.get('chunk_id')
    }

    embedded_targets: set[str] = set()
    for row in filtered_rows:
        for target_id in iter_connected_target_ids(row):
            if target_id in rows_by_chunk_id:
                embedded_targets.add(target_id)

    assembled: list[dict[str, Any]] = []
    for row in filtered_rows:
        if row.get('chunk_id') in embedded_targets:
            continue
        metadata = row.get('chunk_metadata') or {}
        if not isinstance(metadata, dict):
            metadata = {}
        assembled_row = dict(row)
        base_content = str(row.get('content') or '')
        if normalize_chunk_type(row.get('chunk_type')) == 'text':
            connected_targets: list[tuple[int, str]] = []
            for target_id in iter_connected_target_ids(row):
                target_row = rows_by_chunk_id.get(target_id)
                if not target_row:
                    continue
                if normalize_chunk_type(target_row.get('chunk_type')) != 'table':
                    continue
                target_content = str(target_row.get('content') or '').strip()
                if target_content:
                    sort_key = int(target_row.get('sort_order', 0) or 0)
                    connected_targets.append((sort_key, target_content))
            connected_targets.sort(key=lambda x: x[0])
            related_parts = [content for _, content in connected_targets]
            if base_content and related_parts:
                assembled_row['content'] = '\n\n'.join([base_content, *related_parts])
            else:
                assembled_row['content'] = base_content
        else:
            assembled_row['content'] = base_content
        assembled_row['content'] = clean_content(assembled_row['content'])
        assembled.append(assembled_row)
    return assembled


async def hydrate_paths_to_rows(
    db: AsyncSession,
    *,
    path_selections: list[dict[str, Any]],
    user_id: str,
    namespace: str,
    document_id: str | None = None,
) -> list[dict[str, Any]]:
    """Load full chunk rows by section_path or source_chunk_path."""
    if not path_selections:
        return []

    confidence_by_path: dict[str, float] = {}
    mode_by_path: dict[str, str] = {}
    ordered_paths: list[str] = []
    for item in path_selections:
        raw_path = str(item.get('path') or '').strip()
        path = normalize_section_path(raw_path) if raw_path and '/' in raw_path else raw_path
        if not path:
            continue
        confidence = float(item.get('confidence', 0.0) or 0.0)
        hydrate_mode = str(item.get('hydrate_mode') or 'chunks').strip().lower()
        if path not in confidence_by_path:
            ordered_paths.append(path)
            confidence_by_path[path] = confidence
            mode_by_path[path] = hydrate_mode
        else:
            confidence_by_path[path] = max(confidence_by_path[path], confidence)
    if not ordered_paths:
        return []

    outline_paths = [p for p in ordered_paths if mode_by_path.get(p) == 'outline']
    chunk_paths = [p for p in ordered_paths if mode_by_path.get(p) != 'outline']

    rows: list[dict[str, Any]] = []

    if outline_paths:
        outline_section_filters = [
            DocumentSection.section_path == path
            for path in outline_paths
        ]
        outline_stmt = (
            select(Document, DocumentSection)
            .join(
                DocumentSection,
                (DocumentSection.document_id == Document.document_id)
                & (DocumentSection.job_result_id == Document.current_job_result_id),
            )
            .where(Document.user_id == user_id)
            .where(Document.namespace == namespace)
            .where(Document.status == 'active')
            .where(or_(*outline_section_filters))
        )
        if document_id:
            outline_stmt = outline_stmt.where(Document.document_id == document_id)
        outline_result = await db.execute(outline_stmt)
        for document, section in outline_result.all():
            agent_score = confidence_by_path.get(section.section_path, 0.0)
            summary_text = (section.summary or '').strip()
            title_text = (section.section_title or '').strip()
            content = f'[Outline] {title_text}'
            if summary_text:
                content += f'\n{summary_text}'
            rows.append({
                'document_id': document.document_id,
                'chunk_id': f'outline_{section.section_id}',
                'section_id': section.section_id,
                'section_path': section.section_path,
                'source_file_name': document.source_file_name,
                'chunk_type': 'outline',
                'content': content,
                'score': agent_score,
                'agent_score': agent_score,
                'file_path': None,
                'chunk_metadata': {},
                'job_result_id': section.job_result_id,
                'job_id': None,
                'source_chunk_path': None,
                'sort_order': section.sort_order,
                'hydrate_mode': 'outline',
            })

    if chunk_paths:
        section_path_filters = []
        self_only_paths = {p for p in chunk_paths if mode_by_path.get(p) == 'self_only'}
        for path in chunk_paths:
            section_path_filters.append(DocumentSection.section_path == path)
            if path not in self_only_paths:
                section_path_filters.append(DocumentSection.section_path.like(f'{path} / %'))

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
            .where(
                or_(
                    *section_path_filters,
                    DocumentChunk.source_chunk_path.in_(chunk_paths),
                )
            )
        )
        if document_id:
            stmt = stmt.where(Document.document_id == document_id)
        result = await db.execute(stmt)

        mode_allowed_types: dict[str, set[str] | None] = {
            'chunks': None,
            'self_only': None,
            'assets_only': {'image', 'table'},
            'image_only': {'image'},
            'table_only': {'table'},
        }

        seen_paths: set[str] = set()
        for document, chunk, section, job_result in result.all():
            row_path = (section.section_path if section else None) or chunk.source_chunk_path or ''
            if row_path in seen_paths:
                continue

            matched_path = row_path
            if section and section.section_path not in confidence_by_path:
                matched_path = next(
                    (
                        path for path in chunk_paths
                        if section.section_path == path or section.section_path.startswith(f'{path} / ')
                    ),
                    row_path,
                )

            path_mode = mode_by_path.get(matched_path, 'chunks')
            allowed_types = mode_allowed_types.get(path_mode)
            if allowed_types is not None:
                chunk_type_lower = (chunk.chunk_type or '').strip().lower()
                if chunk_type_lower not in allowed_types:
                    continue

            seen_paths.add(row_path)
            agent_score = confidence_by_path.get(matched_path, 0.0)
            rows.append({
                'document_id': document.document_id,
                'chunk_id': chunk.chunk_id,
                'section_id': chunk.section_id,
                'section_path': section.section_path if section else None,
                'source_file_name': document.source_file_name,
                'chunk_type': chunk.chunk_type,
                'content': chunk.content,
                'score': agent_score,
                'agent_score': agent_score,
                'file_path': chunk.file_path,
                'chunk_metadata': chunk.chunk_metadata or {},
                'job_result_id': chunk.job_result_id,
                'job_id': job_result.job_id if job_result else None,
                'source_chunk_path': chunk.source_chunk_path,
                'sort_order': chunk.sort_order,
                'hydrate_mode': path_mode,
            })

    path_order = {path: index for index, path in enumerate(ordered_paths)}

    def _row_sort_key(row: dict[str, Any]) -> int:
        row_path = get_row_path(row)
        if row_path in path_order:
            return path_order[row_path]
        for path, index in path_order.items():
            if row_path.startswith(f'{path} / '):
                return index
        return 10**9

    rows.sort(key=_row_sort_key)
    hydrated_paths = {get_row_path(row) for row in rows}
    resolved_inputs = {
        path for path in ordered_paths
        if path in hydrated_paths
        or any(row_path.startswith(f'{path} / ') for row_path in hydrated_paths)
    }
    resolved_inputs |= set(outline_paths)
    missed = len(ordered_paths) - len(resolved_inputs)
    if missed > 0:
        missing_paths = [path for path in ordered_paths if path not in resolved_inputs]
        logger.warning(
            f'  hydrate: {len(rows)}/{len(ordered_paths)} paths resolved (missed={missed}); '
            f'missing[:5]={missing_paths[:5]}'
        )
    else:
        logger.info(f'  hydrate: {len(rows)}/{len(ordered_paths)} paths resolved')
    return rows
