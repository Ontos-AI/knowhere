from __future__ import annotations

import math
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.document import RetrievalHitStat
from shared.services.retrieval.stats.service import compute_importance_score
from shared.services.retrieval.search.scoring import get_row_path


def get_candidate_key(row: dict[str, Any]) -> str:
    path = get_row_path(row)
    if path:
        return f'path:{path}'
    chunk_id = str(row.get('chunk_id') or '').strip()
    return f'chunk:{chunk_id}' if chunk_id else ''


async def load_chunk_importance_scores(
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


def apply_importance_multiplier(
    rows: list[dict[str, Any]],
    *,
    raw_field: str = 'importance_raw_score',
    low: float = 0.1,
    high: float = 2.0,
) -> None:
    if not rows:
        return

    values = sorted(float(row.get(raw_field, 0.0) or 0.0) for row in rows)
    item_count = len(values)
    median = values[item_count // 2] if item_count % 2 else (values[item_count // 2 - 1] + values[item_count // 2]) / 2
    q1 = values[item_count // 4] if item_count >= 4 else values[0]
    q3 = values[3 * item_count // 4] if item_count >= 4 else values[-1]
    iqr = q3 - q1

    for row in rows:
        raw_score = float(row.get(raw_field, 0.0) or 0.0)
        if iqr <= 1e-9:
            multiplier = 1.0
        else:
            z_score = (raw_score - median) / iqr
            sigmoid_score = 1.0 / (1.0 + math.exp(-z_score))
            multiplier = low + (high - low) * sigmoid_score
        row['importance_multiplier'] = round(multiplier, 4)
        row['agent_score'] = round(
            float(row.get('agent_score', 0.0) or 0.0) * multiplier,
            6,
        )
        row['discovery_score'] = round(
            float(row.get('discovery_score', 0.0) or 0.0) * multiplier,
            6,
        )


def rank_candidates_by_path(
    discovery_rows: list[dict[str, Any]],
    routed_rows: list[dict[str, Any]],
    top_k: int,
    *,
    importance_scores: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    insertion_order: dict[str, int] = {}
    counter = 0

    for row in discovery_rows:
        key = get_candidate_key(row)
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
        key = get_candidate_key(row)
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

    for row in merged.values():
        row['importance_raw_score'] = float(
            (importance_scores or {}).get(str(row.get('chunk_id') or ''), 0.0) or 0.0
        )
    apply_importance_multiplier(list(merged.values()))

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

    def get_sort_key(row: dict[str, Any]) -> tuple[float, float, int]:
        return (
            float(row.get('agent_score', 0.0) or 0.0),
            float(row.get('discovery_score', 0.0) or 0.0),
            -int(row.get('_candidate_order', 0) or 0),
        )

    primary_rows.sort(key=get_sort_key, reverse=True)
    ranked_rows = primary_rows[:top_k]

    if len(ranked_rows) < top_k and fallback_rows:
        fallback_rows.sort(key=get_sort_key, reverse=True)
        ranked_rows.extend(fallback_rows[:top_k - len(ranked_rows)])

    for row in ranked_rows:
        row.pop('_candidate_order', None)
    return ranked_rows


async def rank_retrieval_candidates(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    discovery_rows: list[dict[str, Any]],
    routed_rows: list[dict[str, Any]],
    top_k: int,
) -> list[dict[str, Any]]:
    try:
        importance_scores = await load_chunk_importance_scores(
            db,
            user_id=user_id,
            namespace=namespace,
            rows=[*discovery_rows, *routed_rows],
        )
    except Exception as exc:
        logger.warning(f'Failed to load chunk importance scores, continuing without importance: {exc}')
        importance_scores = {}
    return rank_candidates_by_path(
        discovery_rows,
        routed_rows,
        top_k,
        importance_scores=importance_scores,
    )
