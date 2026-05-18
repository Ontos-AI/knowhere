from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult
from shared.services.retrieval.search.lexical_text import normalize_section_path
from shared.services.retrieval.search.scoring import get_row_path


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

    outline_paths = [path for path in ordered_paths if mode_by_path.get(path) == 'outline']
    chunk_paths = [path for path in ordered_paths if mode_by_path.get(path) != 'outline']

    rows: list[dict[str, Any]] = []

    if outline_paths:
        rows.extend(
            await _hydrate_outline_paths(
                db,
                outline_paths=outline_paths,
                confidence_by_path=confidence_by_path,
                user_id=user_id,
                namespace=namespace,
                document_id=document_id,
            )
        )

    if chunk_paths:
        rows.extend(
            await _hydrate_chunk_paths(
                db,
                chunk_paths=chunk_paths,
                confidence_by_path=confidence_by_path,
                mode_by_path=mode_by_path,
                user_id=user_id,
                namespace=namespace,
                document_id=document_id,
            )
        )

    _sort_rows_by_selection_order(rows, ordered_paths)
    _log_hydration_resolution(rows=rows, ordered_paths=ordered_paths, outline_paths=outline_paths)
    return rows


async def _hydrate_outline_paths(
    db: AsyncSession,
    *,
    outline_paths: list[str],
    confidence_by_path: dict[str, float],
    user_id: str,
    namespace: str,
    document_id: str | None,
) -> list[dict[str, Any]]:
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

    rows: list[dict[str, Any]] = []
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
    return rows


async def _hydrate_chunk_paths(
    db: AsyncSession,
    *,
    chunk_paths: list[str],
    confidence_by_path: dict[str, float],
    mode_by_path: dict[str, str],
    user_id: str,
    namespace: str,
    document_id: str | None,
) -> list[dict[str, Any]]:
    section_path_filters = []
    self_only_paths = {path for path in chunk_paths if mode_by_path.get(path) == 'self_only'}
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

    rows: list[dict[str, Any]] = []
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
                    if section.section_path == path
                    or section.section_path.startswith(f'{path} / ')
                ),
                row_path,
            )

        path_mode = mode_by_path.get(matched_path, 'chunks')
        allowed_types = _get_allowed_types_for_mode(path_mode)
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
    return rows


def _get_allowed_types_for_mode(path_mode: str) -> set[str] | None:
    mode_allowed_types: dict[str, set[str] | None] = {
        'chunks': None,
        'self_only': None,
        'assets_only': {'image', 'table'},
        'image_only': {'image'},
        'table_only': {'table'},
    }
    return mode_allowed_types.get(path_mode)


def _sort_rows_by_selection_order(rows: list[dict[str, Any]], ordered_paths: list[str]) -> None:
    path_order = {path: index for index, path in enumerate(ordered_paths)}

    def row_sort_key(row: dict[str, Any]) -> int:
        row_path = get_row_path(row)
        if row_path in path_order:
            return path_order[row_path]
        for path, index in path_order.items():
            if row_path.startswith(f'{path} / '):
                return index
        return 10**9

    rows.sort(key=row_sort_key)


def _log_hydration_resolution(
    *, rows: list[dict[str, Any]], ordered_paths: list[str], outline_paths: list[str]
) -> None:
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
