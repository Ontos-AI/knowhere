from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.demo_authorization import authorize_demo_caller
from sqlalchemy.ext.asyncio import AsyncSession

# A document's importance for ranking: usage heat from its recorded uses plus
# freshness from its upload, both halving every 30 days. A use counts at most once per UTC day, and n / (n + 2) caps the count
# so a document returned often cannot outweigh relevance.
_HALF_LIFE_DAYS = 30.0
_USAGE_WEIGHT = 0.7
_FRESHNESS_WEIGHT = 0.3
# Ranking multiplies relevance by a factor of this fixed range, from each
# candidate's own importance, matching Memento's memory decay factor.
_MULTIPLIER_LOW = 0.7
_MULTIPLIER_HIGH = 1.5


def _as_utc_naive(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _half_life_decay(moment: datetime, now: datetime) -> float:
    days = max(0.0, (now - _as_utc_naive(moment)).total_seconds() / 86400)
    return math.exp(-math.log(2) * days / _HALF_LIFE_DAYS)


def compute_importance_score(
    hit_count: int, last_hit_at: datetime | None, uploaded_at: datetime
) -> float:
    """Importance in [0, 1]: 0.7 x capped usage heat + 0.3 x upload freshness."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    usage_heat = 0.0
    if hit_count > 0 and last_hit_at is not None:
        usage_heat = hit_count / (hit_count + 2) * _half_life_decay(last_hit_at, now)
    freshness = _half_life_decay(uploaded_at, now)
    return round(_USAGE_WEIGHT * usage_heat + _FRESHNESS_WEIGHT * freshness, 4)


def compute_importance_multiplier(importance: float) -> float:
    """Map importance in [0, 1] to the fixed ranking factor range."""
    bounded = min(1.0, max(0.0, importance))
    return round(_MULTIPLIER_LOW + bounded * (_MULTIPLIER_HIGH - _MULTIPLIER_LOW), 4)


_UPSERT_DOCUMENT_HIT_SQL = text("""
    INSERT INTO retrieval_hit_stats (
        id, user_id, namespace, hit_kind, document_id, chunk_id,
        hit_count, last_hit_at, created_at, updated_at
    )
    VALUES (:id, :user_id, :namespace, :hit_kind, :document_id, NULL, 1, :now, :now, :now)
    ON CONFLICT (user_id, namespace, hit_kind, document_id)
    WHERE chunk_id IS NULL
    DO UPDATE SET
        hit_count = CASE
            WHEN retrieval_hit_stats.last_hit_at >= date_trunc('day', EXCLUDED.last_hit_at)
            THEN retrieval_hit_stats.hit_count
            ELSE retrieval_hit_stats.hit_count + 1
        END,
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
        hit_count = CASE
            WHEN retrieval_hit_stats.last_hit_at >= date_trunc('day', EXCLUDED.last_hit_at)
            THEN retrieval_hit_stats.hit_count
            ELSE retrieval_hit_stats.hit_count + 1
        END,
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
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
    if corpusStorage.is_demo:
        await db.run_sync(lambda session: authorize_demo_caller(session, user_id=user_id))
    params = {
        "id": f"rhs_{uuid4().hex[:12]}",
        "user_id": user_id,
        "namespace": namespace,
        "hit_kind": hit_kind,
        "document_id": document_id,
        "now": now,
    }
    if chunk_id is None:
        await db.execute(text(corpusStorage.compile_sql(str(_UPSERT_DOCUMENT_HIT_SQL))), params)
    else:
        params["chunk_id"] = chunk_id
        await db.execute(text(corpusStorage.compile_sql(str(_UPSERT_CHUNK_HIT_SQL))), params)


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


async def record_document_usage(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    items: list[dict[str, Any]],
) -> int:
    """Record that the caller used these documents, and chunks where given.

    Only active documents the caller owns in this namespace count; any other
    item is ignored. Returns how many items were recorded.
    """
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(namespace)
    if corpusStorage.is_demo:
        await db.run_sync(lambda session: authorize_demo_caller(session, user_id=user_id))
    document_ids = sorted(
        {str(item.get("document_id") or "").strip() for item in items} - {""}
    )
    if not document_ids:
        return 0
    owned = await db.execute(
        select(corpusStorage.Document.document_id)
        .where(corpusStorage.Document.user_id == corpusStorage.resolve_owner(user_id))
        .where(corpusStorage.Document.namespace == namespace)
        .where(corpusStorage.Document.status == "active")
        .where(corpusStorage.Document.document_id.in_(document_ids))
    )
    owned_document_ids = {str(document_id) for document_id in owned.scalars().all()}
    named = [
        (str(item.get("document_id") or "").strip(), str(item.get("chunk_id") or "").strip())
        for item in items
    ]
    results = [
        {"document_id": document_id, "chunk_id": chunk_id or None}
        for document_id, chunk_id in named
        if document_id in owned_document_ids
    ]
    if results:
        await record_retrieval_hits(db, user_id=user_id, namespace=namespace, results=results)
    return len(results)
