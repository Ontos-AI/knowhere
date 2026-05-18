from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_HALF_LIFE_DAYS = 30.0
_ALPHA = 0.7
_BETA = 0.3


def _decay_score(hit_count: int, ref_time: datetime, half_life_days: float) -> float:
    days = (
        datetime.now(timezone.utc).replace(tzinfo=None) - ref_time
    ).total_seconds() / 86400
    return hit_count * math.exp(-0.693 * days / half_life_days)


def compute_importance_score(
    hit_count: int, last_hit_at: datetime, created_at: datetime
) -> float:
    usage_heat = _decay_score(hit_count, last_hit_at, _HALF_LIFE_DAYS)
    freshness = _decay_score(1, created_at, _HALF_LIFE_DAYS)
    return round(_ALPHA * usage_heat + _BETA * freshness, 4)


_UPSERT_DOCUMENT_HIT_SQL = text("""
    INSERT INTO retrieval_hit_stats (
        id, user_id, namespace, hit_kind, document_id, chunk_id,
        hit_count, last_hit_at, created_at, updated_at
    )
    VALUES (:id, :user_id, :namespace, :hit_kind, :document_id, NULL, 1, :now, :now, :now)
    ON CONFLICT (user_id, namespace, hit_kind, document_id)
    WHERE chunk_id IS NULL
    DO UPDATE SET
        hit_count = retrieval_hit_stats.hit_count + 1,
        last_hit_at = EXCLUDED.last_hit_at,
        updated_at = EXCLUDED.updated_at
""")

_UPSERT_CHUNK_HIT_SQL = text("""
    INSERT INTO retrieval_hit_stats (
        id, user_id, namespace, hit_kind, document_id, chunk_id,
        hit_count, last_hit_at, created_at, updated_at
    )
    VALUES (:id, :user_id, :namespace, :hit_kind, :document_id, :chunk_id, 1, :now, :now, :now)
    ON CONFLICT (user_id, namespace, hit_kind, document_id, chunk_id)
    WHERE chunk_id IS NOT NULL
    DO UPDATE SET
        hit_count = retrieval_hit_stats.hit_count + 1,
        last_hit_at = EXCLUDED.last_hit_at,
        updated_at = EXCLUDED.updated_at
""")


async def _upsert_hit_stat(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    hit_kind: str,
    document_id: str,
    chunk_id: str | None,
    now: datetime,
) -> None:
    params = {
        "id": f"rhs_{uuid4().hex[:12]}",
        "user_id": user_id,
        "namespace": namespace,
        "hit_kind": hit_kind,
        "document_id": document_id,
        "now": now,
    }
    if chunk_id is None:
        await db.execute(_UPSERT_DOCUMENT_HIT_SQL, params)
    else:
        params["chunk_id"] = chunk_id
        await db.execute(_UPSERT_CHUNK_HIT_SQL, params)


async def record_retrieval_hits(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    results: list[dict[str, Any]],
) -> None:
    seen_documents: set[str] = set()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for row in results:
        document_id = row.get("document_id")
        chunk_id = row.get("chunk_id")
        if not document_id:
            continue
        if document_id not in seen_documents:
            await _upsert_hit_stat(
                db,
                user_id=user_id,
                namespace=namespace,
                hit_kind="document",
                document_id=document_id,
                chunk_id=None,
                now=now,
            )
            seen_documents.add(document_id)
        if chunk_id:
            await _upsert_hit_stat(
                db,
                user_id=user_id,
                namespace=namespace,
                hit_kind="chunk",
                document_id=document_id,
                chunk_id=chunk_id,
                now=now,
            )

    await db.flush()
