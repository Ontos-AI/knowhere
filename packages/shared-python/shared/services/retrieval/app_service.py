from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from loguru import logger
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.database import get_db_context
from shared.models.database.document import Document, DocumentChunk, DocumentSection, RetrievalHitStat
from shared.services.retrieval.graph_service import GraphQueryService, is_excluded_section
from shared.services.retrieval.lexical_text import normalize_section_path
from shared.services.retrieval.cache_service import get_cached_retrieval_query_result, set_cached_retrieval_query_result
from shared.services.retrieval.hit_stats_service import compute_importance_score, record_retrieval_hits
from shared.services.retrieval.channels import path_channel, content_channel, term_channel
from shared.services.storage.result_storage import get_result_storage
from shared.models.database.job_result import JobResult


_MEDIA_CHUNK_TYPES = {'image', 'table'}

_RRF_K = 60
_CHANNEL_WEIGHT_PATH = 1.0
_CHANNEL_WEIGHT_CONTENT = 2.0
_CHANNEL_WEIGHT_TERM = 1.5
_INTERNAL_RECALL_K_MULTIPLIER = 2
_pending_retrieval_hit_stat_tasks: set[asyncio.Task[None]] = set()

_DATA_TYPE_ALLOWED_CHUNK_TYPES: dict[int, set[str] | None] = {
    1: None,
    2: {'text'},
    3: {'image'},
    4: {'table'},
    5: {'text', 'image'},
    6: {'text', 'table'},
}


def _resolve_allowed_chunk_types(data_type: int) -> set[str] | None:
    return _DATA_TYPE_ALLOWED_CHUNK_TYPES.get(data_type)


_PATH_REF_RE = re.compile(r'\[(?:images|tables)/[^\]\n]+\]')


def _clean_content(content: str) -> str:
    return _PATH_REF_RE.sub('', content).strip()

_PUBLIC_RESULT_FIELDS = {
    'chunk_type', 'content', 'score', 'asset_url',
}

_PUBLIC_SOURCE_FIELDS = {
    'document_id', 'source_file_name', 'section_path',
}


def _normalize_chunk_type(raw: str | None) -> str:
    return str(raw or '').strip().split('\n', 1)[0].lower()


def _filter_excluded_rows(
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


def _iter_connected_target_ids(row: dict[str, Any]) -> list[str]:
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
        if _normalize_chunk_type(row.get('chunk_type')) != 'text':
            continue
        document_id = str(row.get('document_id') or '').strip()
        job_result_id = str(row.get('job_result_id') or '').strip()
        if not document_id or not job_result_id:
            continue
        for target_id in _iter_connected_target_ids(row):
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

    return _filter_excluded_rows(
        hydrated_rows,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
    )


async def assemble_retrieval_results(
    *,
    db: AsyncSession | None = None,
    rows: list[dict[str, Any]],
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    allowed_chunk_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    filtered_rows = _filter_excluded_rows(
        rows,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
    )
    if allowed_chunk_types is not None:
        filtered_rows = [
            row for row in filtered_rows
            if _normalize_chunk_type(row.get('chunk_type')) in allowed_chunk_types
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
        for target_id in _iter_connected_target_ids(row):
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
        if _normalize_chunk_type(row.get('chunk_type')) == 'text':
            connected_targets: list[tuple[int, str]] = []
            for target_id in _iter_connected_target_ids(row):
                target_row = rows_by_chunk_id.get(target_id)
                if not target_row:
                    continue
                if _normalize_chunk_type(target_row.get('chunk_type')) != 'table':
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
        assembled_row['content'] = _clean_content(assembled_row['content'])
        assembled.append(assembled_row)
    return assembled





def _merge_same_section_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return rows
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        sp = row.get('section_path')
        if sp:
            key = f"{row.get('document_id', '')}::{sp}"
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
        base['content'] = '\n'.join(str(r.get('content', '')) for r in group)
        base['score'] = max(r.get('score', 0.0) for r in group)
        merged.append(base)
    return merged


def merge_channels_rrf(
    channels: list[list[dict[str, Any]]],
    weights: list[float],
    top_k: int,
    k: int = _RRF_K,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion across multiple retrieval channels."""
    score_dict: dict[str, float] = {}
    row_by_chunk_id: dict[str, dict[str, Any]] = {}

    for channel_idx, channel_rows in enumerate(channels):
        w = weights[channel_idx] if channel_idx < len(weights) else 1.0
        for rank, row in enumerate(channel_rows):
            chunk_id = str(row.get('chunk_id') or '')
            if not chunk_id:
                continue
            rrf_score = w / (k + rank + 1)
            score_dict[chunk_id] = score_dict.get(chunk_id, 0.0) + rrf_score
            if chunk_id not in row_by_chunk_id:
                row_by_chunk_id[chunk_id] = row

    ranked = sorted(score_dict.items(), key=lambda x: x[1], reverse=True)
    results: list[dict[str, Any]] = []
    for chunk_id, fused_score in ranked[:top_k]:
        row = row_by_chunk_id[chunk_id]
        results.append(dict(row, score=round(fused_score, 6)))
    return results


async def list_graph_routed_chunks(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
) -> list[dict[str, Any]]:
    service = GraphQueryService()
    entry_document_ids = await service.find_entry_documents(
        db,
        user_id=user_id,
        namespace=namespace,
        query=query,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
    )
    return await service.collect_candidate_chunks(
        db,
        user_id=user_id,
        namespace=namespace,
        entry_document_ids=entry_document_ids,
        query=query,
        top_k=top_k * _INTERNAL_RECALL_K_MULTIPLIER,
        exclude_sections=exclude_sections,
    )


def _finalize_retrieval_hit_stats_task(task: asyncio.Task[None]) -> None:
    _pending_retrieval_hit_stat_tasks.discard(task)

    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f'Failed to record retrieval hit stats (ignored): {e}')


def schedule_retrieval_hit_stats_update(*, user_id: str, namespace: str, results: list[dict[str, Any]]) -> None:
    try:
        task = asyncio.create_task(
            _record_retrieval_hit_stats_best_effort(
                user_id=user_id,
                namespace=namespace,
                results=results,
            ),
            name=f'retrieval_hit_stats:{user_id}:{namespace}',
        )
        _pending_retrieval_hit_stat_tasks.add(task)
        task.add_done_callback(_finalize_retrieval_hit_stats_task)
    except Exception as e:
        logger.warning(f'Failed to schedule retrieval hit stats update (ignored): {e}')


async def drain_retrieval_hit_stats_updates(timeout_seconds: float = 2.0) -> None:
    if not _pending_retrieval_hit_stat_tasks:
        return

    pending_tasks = tuple(_pending_retrieval_hit_stat_tasks)

    try:
        await asyncio.wait_for(
            asyncio.gather(*pending_tasks, return_exceptions=True),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        for task in pending_tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*pending_tasks, return_exceptions=True)


async def _record_retrieval_hit_stats_best_effort(*, user_id: str, namespace: str, results: list[dict[str, Any]]) -> None:
    try:
        async with get_db_context() as db:
            await record_retrieval_hits(db, user_id=user_id, namespace=namespace, results=results)
            await db.commit()
    except Exception as e:
        logger.warning(f'Failed to record retrieval hit stats (ignored): {e}')


def _with_citation(row: dict[str, Any]) -> dict[str, Any]:
    citation = {
        'document_id': row.get('document_id'),
        'chunk_id': row.get('chunk_id'),
        'source_file_name': row.get('source_file_name'),
        'section_path': row.get('section_path'),
    }
    return {**row, 'citation': citation}


def _to_public_source(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in _PUBLIC_SOURCE_FIELDS}


def _is_media_chunk(row: dict[str, Any]) -> bool:
    return _normalize_chunk_type(row.get('chunk_type')) in _MEDIA_CHUNK_TYPES


async def generate_retrieval_asset_url(*, job_id: str, artifact_ref: str) -> str | None:
    return get_result_storage().generate_artifact_url(job_id=job_id, artifact_ref=artifact_ref)


def _is_client_result_artifact_ref(asset_ref: str | None) -> bool:
    return get_result_storage().normalize_artifact_ref(asset_ref) is not None


async def _to_public_response(response: dict[str, Any]) -> dict[str, Any]:
    public_response = {
        'namespace': response.get('namespace'),
        'query': response.get('query'),
        'router_used': response.get('router_used'),
        'results': [],
    }

    # Forward agentic evidence fields when present
    if response.get('evidence_text') is not None:
        public_response['evidence_text'] = response['evidence_text']
    if response.get('answer_text') is not None:
        public_response['answer_text'] = response['answer_text']
    if response.get('referenced_chunks') is not None:
        public_response['referenced_chunks'] = response['referenced_chunks']

    public_results: list[dict[str, Any]] = []
    for row in response.get('results', []):
        artifact_ref = row.get('file_path')
        asset_url = None
        if _is_media_chunk(row) and _is_client_result_artifact_ref(artifact_ref) and row.get('job_id'):
            try:
                asset_url = await generate_retrieval_asset_url(
                    job_id=str(row['job_id']),
                    artifact_ref=str(artifact_ref),
                )
            except Exception as e:
                logger.warning(f'Failed to generate retrieval asset URL (ignored): {e}')

        public_row: dict[str, Any] = {}
        for field in _PUBLIC_RESULT_FIELDS:
            if field == 'asset_url':
                if asset_url:
                    public_row['asset_url'] = asset_url
            elif field in row:
                public_row[field] = row[field]
        if 'source' in row:
            public_row['source'] = row['source']
        else:
            public_row['source'] = _to_public_source(row)
        public_results.append(public_row)

    public_response['results'] = public_results
    return public_response


def _get_row_path(row: dict[str, Any]) -> str:
    """Extract the canonical path from a row for deduplication."""
    return str(row.get('section_path') or row.get('source_chunk_path') or '')


def _get_candidate_key(row: dict[str, Any]) -> str:
    path = _get_row_path(row)
    if path:
        return f'path:{path}'
    chunk_id = str(row.get('chunk_id') or '').strip()
    return f'chunk:{chunk_id}' if chunk_id else ''


def _normalize_row_scores(
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


def _importance_multiplier(
    rows: list[dict[str, Any]],
    *,
    raw_field: str = 'importance_raw_score',
    low: float = 0.1,
    high: float = 2.0,
) -> None:
    """Apply adaptive sigmoid-based importance boost to agent/discovery scores.

    Uses median of ``raw_field`` as center and IQR as spread so the curve
    adapts to any KB size without hard-coded thresholds.  When all values
    are identical (IQR ≈ 0) the multiplier is 1.0 (neutral).

    Output range ``[low, high]`` — default [0.1, 2.0] — is the only
    configured constant: max 2× boost, min 10%.  The function modifies
    ``agent_score`` and ``discovery_score`` **in place**.
    """
    import math

    if not rows:
        return

    values = sorted(float(r.get(raw_field, 0.0) or 0.0) for r in rows)
    n = len(values)
    median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
    q1 = values[n // 4] if n >= 4 else values[0]
    q3 = values[3 * n // 4] if n >= 4 else values[-1]
    iqr = q3 - q1

    for row in rows:
        raw = float(row.get(raw_field, 0.0) or 0.0)
        if iqr <= 1e-9:
            mult = 1.0
        else:
            z = (raw - median) / iqr
            s = 1.0 / (1.0 + math.exp(-z))
            mult = low + (high - low) * s
        row['importance_multiplier'] = round(mult, 4)
        row['agent_score'] = round(
            float(row.get('agent_score', 0.0) or 0.0) * mult, 6,
        )
        row['discovery_score'] = round(
            float(row.get('discovery_score', 0.0) or 0.0) * mult, 6,
        )


async def _load_chunk_importance_scores(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    rows: list[dict[str, Any]],
) -> dict[str, float]:
    chunk_ids = sorted({
        str(row.get('chunk_id') or '').strip()
        for row in rows
        if row.get('chunk_id')
    })
    if not chunk_ids:
        return {}
    stmt = (
        select(
            RetrievalHitStat.chunk_id,
            RetrievalHitStat.hit_count,
            RetrievalHitStat.last_hit_at,
            RetrievalHitStat.created_at,
        )
        .where(RetrievalHitStat.user_id == user_id)
        .where(RetrievalHitStat.namespace == namespace)
        .where(RetrievalHitStat.hit_kind == 'chunk')
        .where(RetrievalHitStat.chunk_id.in_(chunk_ids))
    )
    result = await db.execute(stmt)
    importance_scores: dict[str, float] = {}
    for chunk_id, hit_count, last_hit_at, created_at in result.all():
        if not chunk_id:
            continue
        importance_scores[str(chunk_id)] = compute_importance_score(hit_count, last_hit_at, created_at)
    return importance_scores


def _rank_candidates_by_path(
    discovery_rows: list[dict[str, Any]],
    routed_rows: list[dict[str, Any]],
    top_k: int,
) -> list[dict[str, Any]]:
    """Rank discovery and routed candidates in one comparable path space."""
    merged: dict[str, dict[str, Any]] = {}
    insertion_order: dict[str, int] = {}
    counter = 0

    for row in discovery_rows:
        key = _get_candidate_key(row)
        if not key:
            continue
        candidate = dict(row)
        candidate['discovery_score'] = float(row.get('discovery_score', 0.0) or 0.0)
        candidate['agent_score'] = 0.0
        candidate.setdefault('hydrate_mode', 'chunks')
        merged[key] = candidate
        insertion_order[key] = counter
        counter += 1

    for row in routed_rows:
        key = _get_candidate_key(row)
        if not key:
            continue
        routed_agent_score = float(row.get('agent_score', 0.0) or 0.0)
        if key not in merged:
            candidate = dict(row)
            candidate['discovery_score'] = float(row.get('discovery_score', 0.0) or 0.0)
            candidate['agent_score'] = routed_agent_score
            merged[key] = candidate
            insertion_order[key] = counter
            counter += 1
            continue
        candidate = merged[key]
        candidate['agent_score'] = max(float(candidate.get('agent_score', 0.0) or 0.0), routed_agent_score)
        if not candidate.get('source_chunk_path') and row.get('source_chunk_path'):
            candidate['source_chunk_path'] = row.get('source_chunk_path')
        if not candidate.get('section_path') and row.get('section_path'):
            candidate['section_path'] = row.get('section_path')

    # ── Dual-priority ranking ────────────────────────────────────────────
    # When the agent produced results (routed_rows non-empty), rows with
    # agent_score=0 are demoted to a fallback pool.  Primary sort is by
    # agent_score (includes importance boost from _importance_multiplier),
    # with discovery_score as tiebreaker.
    has_agent_results = len(routed_rows) > 0

    primary_rows: list[dict[str, Any]] = []
    fallback_rows: list[dict[str, Any]] = []

    for key, row in merged.items():
        agent_score = float(row.get('agent_score', 0.0) or 0.0)
        discovery_score = float(row.get('discovery_score', 0.0) or 0.0)
        row['evidence_score'] = round(agent_score if has_agent_results else max(discovery_score, agent_score), 6)
        row['score'] = row['evidence_score']
        row['_candidate_order'] = insertion_order[key]

        if has_agent_results and agent_score <= 0.0:
            fallback_rows.append(row)
        else:
            primary_rows.append(row)

    def _sort_key(row):
        return (
            float(row.get('agent_score', 0.0) or 0.0),
            float(row.get('discovery_score', 0.0) or 0.0),
            -int(row.get('_candidate_order', 0) or 0),
        )

    primary_rows.sort(key=_sort_key, reverse=True)
    ranked_rows = primary_rows[:top_k]

    # Back-fill from fallback if primary results are insufficient
    if len(ranked_rows) < top_k and fallback_rows:
        fallback_rows.sort(key=_sort_key, reverse=True)
        ranked_rows.extend(fallback_rows[:top_k - len(ranked_rows)])

    for row in ranked_rows:
        row.pop('_candidate_order', None)
    return ranked_rows


async def _count_scoped_chunks(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    exclude_document_ids: list[str],
    allowed_chunk_types: set[str] | None,
) -> int:
    stmt = (
        select(func.count(DocumentChunk.id))
        .join(Document, (Document.document_id == DocumentChunk.document_id) & (Document.current_job_result_id == DocumentChunk.job_result_id))
        .where(Document.user_id == user_id)
        .where(Document.namespace == namespace)
        .where(Document.status == 'active')
    )
    if exclude_document_ids:
        stmt = stmt.where(Document.document_id.notin_(list(exclude_document_ids)))
    if allowed_chunk_types is not None:
        stmt = stmt.where(func.lower(DocumentChunk.chunk_type).in_(list(allowed_chunk_types)))
    result = await db.execute(stmt)
    return result.scalar() or 0


async def _load_all_scoped_chunks(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    allowed_chunk_types: set[str] | None,
    signal_paths: list[str],
    filter_mode: str,
) -> list[dict[str, Any]]:
    stmt = (
        select(Document, DocumentChunk, DocumentSection, JobResult)
        .join(DocumentChunk, (DocumentChunk.document_id == Document.document_id) & (DocumentChunk.job_result_id == Document.current_job_result_id))
        .outerjoin(DocumentSection, DocumentSection.section_id == DocumentChunk.section_id)
        .join(JobResult, JobResult.id == DocumentChunk.job_result_id)
        .where(Document.user_id == user_id)
        .where(Document.namespace == namespace)
        .where(Document.status == 'active')
        .order_by(DocumentChunk.sort_order)
    )
    if exclude_document_ids:
        stmt = stmt.where(Document.document_id.notin_(list(exclude_document_ids)))
    if allowed_chunk_types is not None:
        stmt = stmt.where(func.lower(DocumentChunk.chunk_type).in_(list(allowed_chunk_types)))

    result = await db.execute(stmt)
    rows: list[dict[str, Any]] = []
    for document, chunk, section, job_result in result.all():
        section_path = section.section_path if section else None
        if is_excluded_section(document_id=document.document_id, section_path=section_path, exclude_sections=exclude_sections):
            continue
        if signal_paths and section_path:
            path_lower = section_path.lower()
            matches_any = any(kw.lower() in path_lower for kw in signal_paths)
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
            'file_path': chunk.file_path,
            'chunk_metadata': chunk.chunk_metadata or {},
            'job_result_id': chunk.job_result_id,
            'job_id': job_result.job_id if job_result else None,
            'sort_order': chunk.sort_order,
        })
    return rows


async def _hydrate_paths_to_rows(
    db: AsyncSession,
    *,
    path_selections: list[dict[str, Any]],
    user_id: str,
    namespace: str,
    document_id: str | None = None,
) -> list[dict[str, Any]]:
    """Load full chunk rows by section_path or source_chunk_path.

    When *document_id* is provided the query is scoped to that single
    document, preventing cross-document collisions on generic paths
    such as ``Root``.

    Supports hydrate_mode branching:
      - 'chunks' (default): all chunk types under the section subtree
      - 'outline': synthetic row from section metadata, no real chunks
      - 'assets_only': only image + table chunks
      - 'image_only': only image chunks
      - 'table_only': only table chunks
    """
    if not path_selections:
        return []

    # Group selections by hydrate_mode
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

    # Separate outline paths from chunk-loading paths
    outline_paths = [p for p in ordered_paths if mode_by_path.get(p) == 'outline']
    chunk_paths = [p for p in ordered_paths if mode_by_path.get(p) != 'outline']

    rows: list[dict[str, Any]] = []

    # ── Outline mode: synthesize rows from section metadata ──────────────
    if outline_paths:
        outline_section_filters = []
        for path in outline_paths:
            outline_section_filters.append(DocumentSection.section_path == path)

        outline_stmt = (
            select(Document, DocumentSection)
            .join(DocumentSection, (DocumentSection.document_id == Document.document_id)
                  & (DocumentSection.job_result_id == Document.current_job_result_id))
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

    # ── Chunk modes: load real chunks with optional type filters ─────────
    if chunk_paths:
        section_path_filters = []
        for path in chunk_paths:
            section_path_filters.append(DocumentSection.section_path == path)
            section_path_filters.append(DocumentSection.section_path.like(f'{path} / %'))

        stmt = (
            select(Document, DocumentChunk, DocumentSection, JobResult)
            .join(DocumentChunk, (DocumentChunk.document_id == Document.document_id)
                  & (DocumentChunk.job_result_id == Document.current_job_result_id))
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

        # Build a map of path → allowed chunk_types based on hydrate_mode
        _MODE_ALLOWED_TYPES: dict[str, set[str] | None] = {
            'chunks': None,                       # all types
            'assets_only': {'image', 'table'},
            'image_only': {'image'},
            'table_only': {'table'},
        }

        seen_paths: set[str] = set()
        for document, chunk, section, job_result in result.all():
            row_path = (section.section_path if section else None) or chunk.source_chunk_path or ''
            if row_path in seen_paths:
                continue

            # Find which ordered path this row belongs to
            matched_path = row_path
            if section and section.section_path not in confidence_by_path:
                matched_path = next(
                    (
                        path for path in chunk_paths
                        if section.section_path == path or section.section_path.startswith(f'{path} / ')
                    ),
                    row_path,
                )

            # Check chunk_type filter based on hydrate_mode
            path_mode = mode_by_path.get(matched_path, 'chunks')
            allowed_types = _MODE_ALLOWED_TYPES.get(path_mode)
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

    # ── Sort by agent-selected order ─────────────────────────────────────
    path_order = {p: idx for idx, p in enumerate(ordered_paths)}

    def _row_sort_key(row: dict[str, Any]) -> int:
        row_path = _get_row_path(row)
        if row_path in path_order:
            return path_order[row_path]
        for path, idx in path_order.items():
            if row_path.startswith(f'{path} / '):
                return idx
        return 10**9

    rows.sort(key=_row_sort_key)
    hydrated_paths = {_get_row_path(r) for r in rows}
    resolved_inputs = {
        path for path in ordered_paths
        if path in hydrated_paths or any(row_path.startswith(f'{path} / ') for row_path in hydrated_paths)
    }
    # Outline paths are always resolved (synthesized)
    resolved_inputs |= set(outline_paths)
    missed = len(ordered_paths) - len(resolved_inputs)
    if missed > 0:
        missing_paths = [p for p in ordered_paths if p not in resolved_inputs]
        logger.warning(
            f'  hydrate: {len(rows)}/{len(ordered_paths)} paths resolved (missed={missed}); '
            f'missing[:5]={missing_paths[:5]}'
        )
    else:
        logger.info(f'  hydrate: {len(rows)}/{len(ordered_paths)} paths resolved')
    return rows


async def run_retrieval_query(
    *,
    db: AsyncSession,
    user_id: str,
    namespace: str,
    query: str,
    top_k: int,
    exclude_document_ids: list[str],
    exclude_sections: list[dict[str, str]],
    data_type: int = 1,
    signal_paths: list[str] | None = None,
    filter_mode: str = 'delete',
    channels: list[str] | None = None,
    channel_weights: dict[str, float] | None = None,
    rerank: bool = False,
    threshold: float = 0.0,
    internal_recall_k: int | None = None,
) -> dict[str, Any]:
    """Checkerboard retrieval: 3 independent channels -> RRF -> agent/graph union -> assembly."""
    t_start = time.monotonic()
    query = query.strip()
    logger.info('\n' + '█' * 70)
    logger.info('  🚀 RETRIEVAL PIPELINE START')
    logger.info(f'  query="{query}"')
    logger.info(f'  user={user_id}  ns={namespace}  top_k={top_k}  data_type={data_type}')
    logger.info(f'  exclude_docs={exclude_document_ids}  exclude_secs={len(exclude_sections)}')
    logger.info('█' * 70)

    if not query:
        logger.info('  ⛔ Empty query filtered, skipping retrieval pipeline')
        return {
            "namespace": namespace,
            "query": query,
            "router_used": "empty_query_filtered",
            "results": [],
        }

    allowed_chunk_types = _resolve_allowed_chunk_types(data_type)
    effective_recall_k = internal_recall_k if internal_recall_k is not None else top_k * _INTERNAL_RECALL_K_MULTIPLIER
    logger.info(f'  allowed_chunk_types={allowed_chunk_types}  effective_recall_k={effective_recall_k}  signal_paths={signal_paths}  filter_mode={filter_mode}  rerank={rerank}  threshold={threshold}')

    cache_extra = dict(
        data_type=data_type,
        signal_paths=signal_paths,
        filter_mode=filter_mode,
        channels=channels,
        channel_weights=channel_weights,
        rerank=rerank,
        threshold=threshold,
        internal_recall_k=internal_recall_k,
    )

    cache_version: int | None = None
    try:
        cache_version, cached = await get_cached_retrieval_query_result(
            user_id=user_id,
            namespace=namespace,
            query=query,
            top_k=top_k,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            **cache_extra,
        )
        if cached:
            logger.info(f'retrieval: cache_hit=True version={cache_version}')
            try:
                schedule_retrieval_hit_stats_update(
                    user_id=user_id,
                    namespace=namespace,
                    results=cached.get("results", []),
                )
            except Exception as e:
                logger.warning(f"Failed to trigger retrieval hit stats update (ignored): {e}")
            return await _to_public_response(cached)
    except Exception as e:
        logger.warning(f"Failed to read retrieval cache (ignored): {e}")

    logger.debug(f'  📦 Cache miss (version={cache_version}), running full pipeline')

    # ── Small KB optimization ──
    try:
        total_chunk_count = await _count_scoped_chunks(
            db, user_id=user_id, namespace=namespace,
            exclude_document_ids=exclude_document_ids,
            allowed_chunk_types=allowed_chunk_types,
        )
    except Exception as e:
        logger.warning(f"Failed to count scoped chunks, skipping small KB optimization: {e}")
        total_chunk_count = top_k + 1
    logger.info(f'\n  📊 Total chunks in scope: {total_chunk_count}')
    if total_chunk_count <= top_k:
        logger.info(f'  ⚡ Small KB optimization: {total_chunk_count} chunks <= top_k={top_k}, returning all')
        all_rows = await _load_all_scoped_chunks(
            db, user_id=user_id, namespace=namespace,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            allowed_chunk_types=allowed_chunk_types,
            signal_paths=signal_paths or [],
            filter_mode=filter_mode,
        )
        logger.info(f'  small_kb load: loaded={len(all_rows)} rows after signal/exclude filters')
        assembled_rows = await assemble_retrieval_results(
            db=db, rows=all_rows,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            allowed_chunk_types=allowed_chunk_types,
        )
        results = [_with_citation(row) for row in assembled_rows]
        response = {
            "namespace": namespace, "query": query,
            "router_used": "small_kb_all", "results": results,
        }
        if cache_version is not None:
            try:
                await set_cached_retrieval_query_result(
                    user_id=user_id, namespace=namespace, version=cache_version,
                    query=query, top_k=top_k,
                    exclude_document_ids=exclude_document_ids,
                    exclude_sections=exclude_sections,
                    response=response, **cache_extra,
                )
            except Exception as e:
                logger.warning(f"Failed to write retrieval cache (ignored): {e}")
        try:
            schedule_retrieval_hit_stats_update(user_id=user_id, namespace=namespace, results=results)
        except Exception as e:
            logger.warning(f"Failed to trigger retrieval hit stats update (ignored): {e}")
        elapsed_total = round((time.monotonic() - t_start) * 1000)
        logger.info(f'  ✅ Small KB: {len(results)} results in {elapsed_total}ms')
        return await _to_public_response(response)

    # ══ Route: agentic vs legacy ══
    _agentic_enabled = os.environ.get('RETRIEVAL_AGENTIC_ENABLED', 'false') == 'true'
    if _agentic_enabled:
        # ── AGENTIC path (all errors self-contained, no fallback to legacy) ──
        from shared.services.retrieval.agentic.orchestrator import RetrievalAgent
        from shared.services.retrieval.llm_adapter import create_retrieval_llm_fn as _create_llm

        llm_fn = _create_llm()
        agent = RetrievalAgent()
        agentic_result = await agent.run(
            db,
            user_id=user_id,
            namespace=namespace,
            query=query,
            top_k=top_k,
            llm_fn=llm_fn,
            exclude_document_ids=exclude_document_ids,
            exclude_sections=exclude_sections,
            data_type=data_type,
            signal_paths=signal_paths,
            filter_mode=filter_mode,
            channels=channels,
            channel_weights=channel_weights,
        )
        router_used = agentic_result.router_used

        # Generate asset URLs for media chunks in referenced_chunks
        enriched_refs: list[dict[str, Any]] = []
        for ref in agentic_result.referenced_chunks:
            enriched = dict(ref)
            chunk_type = _normalize_chunk_type(ref.get('chunk_type'))
            artifact_ref = ref.get('file_path', '')
            job_id = ref.get('job_id', '')
            if chunk_type in _MEDIA_CHUNK_TYPES and job_id and _is_client_result_artifact_ref(artifact_ref):
                try:
                    asset_url = await generate_retrieval_asset_url(
                        job_id=str(job_id), artifact_ref=str(artifact_ref),
                    )
                    if asset_url:
                        enriched['asset_url'] = asset_url
                except Exception as e:
                    logger.warning(f'Failed to generate agentic asset URL (ignored): {e}')
            enriched_refs.append(enriched)

        # Build backward-compatible results[] from referenced_chunks
        # (minimal: chunk_id + document_id + chunk_type + section_path)
        results = [
            {
                'chunk_id': ref.get('chunk_id'),
                'document_id': ref.get('document_id'),
                'chunk_type': ref.get('chunk_type'),
                'source': {
                    'document_id': ref.get('document_id'),
                    'section_path': ref.get('section_path'),
                },
            }
            for ref in enriched_refs
        ]

        response = {
            "namespace": namespace,
            "query": query,
            "router_used": router_used,
            "results": results,
            "evidence_text": agentic_result.evidence_text,
            "answer_text": agentic_result.answer_text,
            "referenced_chunks": enriched_refs,
        }

        if cache_version is not None:
            try:
                await set_cached_retrieval_query_result(
                    user_id=user_id, namespace=namespace, version=cache_version,
                    query=query, top_k=top_k,
                    exclude_document_ids=exclude_document_ids,
                    exclude_sections=exclude_sections,
                    response=response, **cache_extra,
                )
            except Exception as e:
                logger.warning(f"Failed to write retrieval cache (ignored): {e}")

        try:
            schedule_retrieval_hit_stats_update(
                user_id=user_id, namespace=namespace,
                results=agentic_result.referenced_chunks,
            )
        except Exception as e:
            logger.warning(f"Failed to trigger retrieval hit stats update (ignored): {e}")

        elapsed_total = round((time.monotonic() - t_start) * 1000)
        logger.info(
            f'\n{"█" * 70}\n'
            f'  ✅ AGENTIC RETRIEVAL COMPLETE: '
            f'{len(enriched_refs)} chunks | '
            f'evidence={len(agentic_result.evidence_text)} chars | '
            f'answer={len(agentic_result.answer_text)} chars | '
            f'router={router_used} | {elapsed_total}ms\n'
            f'{"█" * 70}'
        )

        return await _to_public_response(response)
    else:
        # ── LEGACY path (existing code, unchanged) ──

        # ── Channel execution ──
        active_channels = set(channels) if channels else {'path', 'content', 'term'}
        logger.info(f'\n  📡 PHASE 1: Bottom-Layer Discovery (channels={sorted(active_channels)})')
        logger.info(f'  effective_recall_k={effective_recall_k}')

        path_rows: list[dict[str, Any]] = []
        content_rows: list[dict[str, Any]] = []
        term_rows: list[dict[str, Any]] = []

        if 'path' in active_channels:
            t_ch = time.monotonic()
            path_rows = await path_channel(
                db, user_id=user_id, namespace=namespace, query=query,
                top_k=effective_recall_k, exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections, allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths, filter_mode=filter_mode,
            )
            elapsed_ch = round((time.monotonic() - t_ch) * 1000)
            logger.info(f'\n  📡 path_channel: {len(path_rows)} rows in {elapsed_ch}ms')
            for i, r in enumerate(path_rows[:5]):
                logger.info(f'    [{i}] score={r.get("score",0):.4f}  path={r.get("section_path","") or r.get("source_chunk_path","")}  type={r.get("chunk_type","?")}')
            if len(path_rows) > 5:
                logger.info(f'    ... and {len(path_rows) - 5} more')

        if 'content' in active_channels:
            t_ch = time.monotonic()
            content_rows = await content_channel(
                db, user_id=user_id, namespace=namespace, query=query,
                top_k=effective_recall_k, exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections, allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths, filter_mode=filter_mode,
            )
            elapsed_ch = round((time.monotonic() - t_ch) * 1000)
            logger.info(f'\n  📡 content_channel: {len(content_rows)} rows in {elapsed_ch}ms')
            for i, r in enumerate(content_rows[:5]):
                logger.info(f'    [{i}] score={r.get("score",0):.4f}  path={r.get("section_path","") or r.get("source_chunk_path","")}  content={str(r.get("content",""))[:80]}')
            if len(content_rows) > 5:
                logger.info(f'    ... and {len(content_rows) - 5} more')

        if 'term' in active_channels:
            t_ch = time.monotonic()
            term_rows = await term_channel(
                db, user_id=user_id, namespace=namespace, query=query,
                top_k=effective_recall_k, exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections, allowed_chunk_types=allowed_chunk_types,
                signal_paths=signal_paths, filter_mode=filter_mode,
            )
            elapsed_ch = round((time.monotonic() - t_ch) * 1000)
            logger.info(f'\n  📡 term_channel: {len(term_rows)} rows in {elapsed_ch}ms')
            for i, r in enumerate(term_rows[:5]):
                logger.info(f'    [{i}] score={r.get("score",0):.4f}  path={r.get("section_path","") or r.get("source_chunk_path","")}  type={r.get("chunk_type","?")}')
            if len(term_rows) > 5:
                logger.info(f'    ... and {len(term_rows) - 5} more')

        # ── RRF fusion with configurable weights ──
        default_weights = {
            'path': _CHANNEL_WEIGHT_PATH,
            'content': _CHANNEL_WEIGHT_CONTENT,
            'term': _CHANNEL_WEIGHT_TERM,
        }
        effective_weights = {**default_weights, **(channel_weights or {})}

        channel_lists: list[list[dict[str, Any]]] = []
        weight_list: list[float] = []

        if path_rows:
            channel_lists.append(path_rows)
            weight_list.append(effective_weights.get('path', _CHANNEL_WEIGHT_PATH))
        if content_rows:
            channel_lists.append(content_rows)
            weight_list.append(effective_weights.get('content', _CHANNEL_WEIGHT_CONTENT))
        if term_rows:
            channel_lists.append(term_rows)
            weight_list.append(effective_weights.get('term', _CHANNEL_WEIGHT_TERM))

        fused_rows = merge_channels_rrf(channel_lists, weight_list, top_k) if channel_lists else []
        logger.info(f'\n  🔀 RRF Fusion: {len(fused_rows)} rows from {len(channel_lists)} channels (weights={dict(zip(["path","content","term"][:len(weight_list)], weight_list))})')
        for i, r in enumerate(fused_rows[:5]):
            logger.info(f'    [{i}] rrf_score={r.get("score",0):.4f}  path={r.get("section_path","") or r.get("source_chunk_path","")}')
        if len(fused_rows) > 5:
            logger.info(f'    ... and {len(fused_rows) - 5} more')

        # ── Section merging ──
        pre_merge = len(fused_rows)
        fused_rows = _merge_same_section_rows(fused_rows)
        if len(fused_rows) != pre_merge:
            logger.info(f'retrieval: section_merge={pre_merge}->{len(fused_rows)}')

        # ── Threshold filtering ──
        if threshold > 0.0 and fused_rows:
            pre_count = len(fused_rows)
            fused_rows = [row for row in fused_rows if row.get('score', 0.0) >= threshold]
            logger.info(f'retrieval: threshold_filter={pre_count}->{len(fused_rows)} (threshold={threshold})')

        if fused_rows:
            _normalize_row_scores(
                fused_rows,
                source_field='score',
                target_field='discovery_score',
                default=0.5,
            )

        # ── Legacy graph routing ──
        logger.info('\n  🧭 PHASE 2: Legacy Graph Routing')
        router_used = 'discovery_only'
        agent_rows: list[dict[str, Any]] = []

        try:
            agent_rows = await list_graph_routed_chunks(
                db, user_id=user_id, namespace=namespace, query=query,
                top_k=top_k, exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections,
            )
            if agent_rows:
                router_used = 'discovery+graph'
                logger.info(f'  📊 Graph routing: {len(agent_rows)} rows')
        except Exception as exc:
            logger.error(f'  ❌ Graph routing failed (ignored): {exc}')
            agent_rows = []

        if agent_rows:
            _normalize_row_scores(
                agent_rows,
                source_field='score',
                target_field='agent_score',
                default=0.5,
            )

        combined_rows = [*fused_rows, *agent_rows]
        if combined_rows:
            try:
                chunk_importance_scores = await _load_chunk_importance_scores(
                    db,
                    user_id=user_id,
                    namespace=namespace,
                    rows=combined_rows,
                )
            except Exception as exc:
                logger.warning(f'Failed to load chunk importance scores, continuing without importance: {exc}')
                chunk_importance_scores = {}
            for row in combined_rows:
                row['importance_raw_score'] = float(chunk_importance_scores.get(str(row.get('chunk_id') or ''), 0.0) or 0.0)

        ranked_rows = _rank_candidates_by_path(fused_rows, agent_rows, top_k)
        if ranked_rows:
            logger.info(f'\n  🧮 Unified candidate ranking: {len(ranked_rows)} rows')
            for i, row in enumerate(ranked_rows[:10]):
                logger.info(
                    '    '
                    f'[{i}] evidence={row.get("evidence_score", 0.0):.4f} '
                    f'discovery={row.get("discovery_score", 0.0):.4f} '
                    f'agent={row.get("agent_score", 0.0):.4f} '
                    f'path={_get_row_path(row)}'
                )

    assembled_rows = await assemble_retrieval_results(
        db=db,
        rows=ranked_rows,
        exclude_document_ids=exclude_document_ids,
        exclude_sections=exclude_sections,
        allowed_chunk_types=allowed_chunk_types,
    )
    results = [_with_citation(row) for row in assembled_rows]

    response = {
        "namespace": namespace,
        "query": query,
        "router_used": router_used,
        "results": results,
    }

    if cache_version is not None:
        try:
            await set_cached_retrieval_query_result(
                user_id=user_id,
                namespace=namespace,
                version=cache_version,
                query=query,
                top_k=top_k,
                exclude_document_ids=exclude_document_ids,
                exclude_sections=exclude_sections,
                response=response,
                **cache_extra,
            )
        except Exception as e:
            logger.warning(f"Failed to write retrieval cache (ignored): {e}")

    try:
        schedule_retrieval_hit_stats_update(
            user_id=user_id,
            namespace=namespace,
            results=results,
        )
    except Exception as e:
        logger.warning(f"Failed to trigger retrieval hit stats update (ignored): {e}")

    elapsed_total = round((time.monotonic() - t_start) * 1000)
    logger.info(f'\n{"█" * 70}')
    logger.info(f'  ✅ RETRIEVAL COMPLETE: {len(results)} results | router={router_used} | {elapsed_total}ms')
    for i, r in enumerate(results[:10]):
        src = r.get('source', {})
        logger.info(
            f'    [{i+1}] type={r.get("chunk_type","?")}  score={r.get("score",0):.4f}'
            f'  path={src.get("section_path","")}'
            f'  file={src.get("source_file_name","")}'
        )
    if len(results) > 10:
        logger.info(f'    ... and {len(results) - 10} more')
    logger.info(f'{"█" * 70}')

    return await _to_public_response(response)
