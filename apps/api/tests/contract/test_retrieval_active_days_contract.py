"""Ranking importance ages a namespace's documents by its active days only."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import AsyncClient

from shared.services.retrieval.search.ranking import load_importance_scores
from shared.services.retrieval.stats.active_days import record_active_day
from tests.contract.test_retrieval_classic_map_unit_contract import _USER_ID, _publish_document
from tests.support.contract_database import ContractDatabase
from tests.support.retrieval_snapshot_support import contract_db_session

_TODAY: date = datetime.now(timezone.utc).date()


async def _publish_old_document(namespace: str, chunk_id: str) -> str:
    published = await _publish_document(
        namespace=namespace,
        source_file_name="aging.pdf",
        chunks=[
            {
                "chunk_id": chunk_id,
                "type": "text",
                "content": "aging evidence",
                "path": "aging.pdf/Root/body",
                "order": 1,
            }
        ],
    )
    document_id = published["document_id"]
    await ContractDatabase.execute(
        "UPDATE documents SET created_at = NOW() - interval '100 days' "
        "WHERE document_id = :document_id",
        {"document_id": document_id},
    )
    return document_id


async def _record_days(*, user_id: str, namespace: str, days: list[date]) -> None:
    async with contract_db_session() as db:
        for day in days:
            await record_active_day(db, user_id=user_id, namespace=namespace, day=day)
        await db.commit()


async def _importance(namespace: str, document_id: str, chunk_id: str) -> dict[str, float]:
    async with contract_db_session() as db:
        return await load_importance_scores(
            db,
            user_id=_USER_ID,
            namespace=namespace,
            rows=[{"document_id": document_id, "chunk_id": chunk_id}],
        )


async def test_documents_age_only_by_the_caller_s_active_days_in_the_namespace(
    developer_api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
) -> None:
    namespace = f"aging-{uuid4().hex[:8]}"
    chunk_id = f"{namespace}-body"
    async with developer_api_client_factory():
        document_id = await _publish_old_document(namespace, chunk_id)

        # Nobody searched since the upload, so only today's search adds age.
        idle = await _importance(namespace, document_id, chunk_id)
        assert idle == {f"{document_id}:": pytest.approx(0.3 * 0.5 ** (1 / 30), abs=1e-3)}

        past_days = [_TODAY - timedelta(days=offset) for offset in range(1, 30)]
        await _record_days(user_id=_USER_ID, namespace=namespace, days=past_days)
        await _record_days(user_id=_USER_ID, namespace=namespace, days=past_days[:1])
        await _record_days(user_id="someone-else", namespace=namespace, days=[_TODAY - timedelta(days=40)])
        await _record_days(user_id=_USER_ID, namespace=f"{namespace}-other", days=[_TODAY - timedelta(days=50)])

        month = await _importance(namespace, document_id, chunk_id)
        assert month == {f"{document_id}:": pytest.approx(0.15, abs=1e-3)}

        await ContractDatabase.execute(
            "INSERT INTO retrieval_hit_stats (id, user_id, namespace, hit_kind, document_id, "
            "chunk_id, hit_count, last_hit_at, created_at, updated_at) VALUES "
            "(:id, :user_id, :namespace, 'chunk', :document_id, :chunk_id, 1, "
            "NOW() - interval '200 days', NOW(), NOW())",
            {
                "id": f"rhs_{uuid4().hex[:12]}",
                "user_id": _USER_ID,
                "namespace": namespace,
                "document_id": document_id,
                "chunk_id": chunk_id,
            },
        )
        used = await _importance(namespace, document_id, chunk_id)
        assert used[f"{document_id}:{chunk_id}"] == pytest.approx(
            0.7 * (1 / 3) * 0.5 + 0.3 * 0.5, abs=1e-3
        )
