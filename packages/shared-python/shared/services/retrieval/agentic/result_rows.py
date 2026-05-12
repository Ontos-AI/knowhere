"""Row helpers used by the agentic retrieval pipeline.

These functions intentionally avoid importing ``app_service`` so the agentic
orchestrator can be imported without creating retrieval package cycles.
"""
from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult
from shared.services.retrieval.graph_service import is_excluded_section
from shared.services.retrieval.lexical_text import normalize_section_path
from shared.services.storage.result_storage import get_result_storage

CHANNEL_WEIGHT_PATH = 1.0
CHANNEL_WEIGHT_CONTENT = 2.0
CHANNEL_WEIGHT_TERM = 1.5
INTERNAL_RECALL_K_MULTIPLIER = 2
RRF_K = 60

DATA_TYPE_ALLOWED_CHUNK_TYPES: dict[int, set[str] | None] = {
    1: None,
    2: {'text'},
    3: {'image'},
    4: {'table'},
    5: {'text', 'image'},
    6: {'text', 'table'},
}

MODE_ALLOWED_TYPES: dict[str, set[str] | None] = {
    'chunks': None,
    'assets_only': {'image', 'table'},
    'image_only': {'image'},
    'table_only': {'table'},
}


def resolve_allowed_chunk_types(data_type: int) -> set[str] | None:
    return DATA_TYPE_ALLOWED_CHUNK_TYPES.get(data_type)


def normalize_chunk_type(raw: str | None) -> str:
    return str(raw or '').strip().split('\n', 1)[0].lower()


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


def merge_same_section_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return rows
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        section_path = row.get('section_path')
        if section_path:
            key = f"{row.get('document_id', '')}::{section_path}"
        else:
            key = row.get('chunk_id', '')
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    merged: list[dict[str, Any]] = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            merged.append(group[0])
            continue
        base = dict(group[0])
        base['content'] = '\n'.join(str(row.get('content', '')) for row in group)
        base['score'] = max(row.get('score', 0.0) for row in group)
        merged.append(base)
    return merged


def merge_channels_rrf(
    channels: list[list[dict[str, Any]]],
    weights: list[float],
    top_k: int,
    k: int = RRF_K,
) -> list[dict[str, Any]]:
    score_dict: dict[str, float] = {}
    row_by_chunk_id: dict[str, dict[str, Any]] = {}

    for channel_idx, channel_rows in enumerate(channels):
        weight = weights[channel_idx] if channel_idx < len(weights) else 1.0
        for rank, row in enumerate(channel_rows):
            chunk_id = str(row.get('chunk_id') or '')
            if not chunk_id:
                continue
            rrf_score = weight / (k + rank + 1)
            score_dict[chunk_id] = score_dict.get(chunk_id, 0.0) + rrf_score
            if chunk_id not in row_by_chunk_id:
                row_by_chunk_id[chunk_id] = row

    ranked = sorted(score_dict.items(), key=lambda item: item[1], reverse=True)
    results: list[dict[str, Any]] = []
    for chunk_id, fused_score in ranked[:top_k]:
        row = row_by_chunk_id[chunk_id]
        results.append(dict(row, score=round(fused_score, 6)))
    return results


def normalize_row_scores(
    rows: list[dict[str, Any]],
    *,
    source_field: str,
    target_field: str,
    default: float,
) -> None:
    if not rows:
        return
    values = [float(row.get(source_field, 0.0) or 0.0) for row in rows]
    min_score = min(values)
    max_score = max(values)
    if max_score <= 0.0 and min_score <= 0.0:
        for row in rows:
            row[target_field] = 0.0
        return
    if max_score == min_score:
        for row in rows:
            row[target_field] = default
        return
    denominator = max_score - min_score
    for row in rows:
        raw_score = float(row.get(source_field, 0.0) or 0.0)
        row[target_field] = round((raw_score - min_score) / denominator, 6)


def get_row_path(row: dict[str, Any]) -> str:
    return str(row.get('section_path') or row.get('source_chunk_path') or '')


async def hydrate_paths_to_rows(
    db: AsyncSession,
    *,
    path_selections: list[dict[str, Any]],
    user_id: str,
    namespace: str,
    document_id: str | None = None,
) -> list[dict[str, Any]]:
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
        for path in chunk_paths:
            section_path_filters.append(DocumentSection.section_path == path)
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
            allowed_types = MODE_ALLOWED_TYPES.get(path_mode)
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

    def get_sort_key(row: dict[str, Any]) -> int:
        row_path = get_row_path(row)
        if row_path in path_order:
            return path_order[row_path]
        for path, index in path_order.items():
            if row_path.startswith(f'{path} / '):
                return index
        return 10**9

    rows.sort(key=get_sort_key)
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


async def generate_retrieval_asset_url(*, job_id: str, artifact_ref: str) -> str | None:
    return get_result_storage().generate_artifact_url(job_id=job_id, artifact_ref=artifact_ref)


def is_client_result_artifact_ref(asset_ref: str | None) -> bool:
    return get_result_storage().normalize_artifact_ref(asset_ref) is not None
