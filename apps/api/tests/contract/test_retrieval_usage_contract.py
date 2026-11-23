"""Reported document use is recorded once per UTC day, for owned documents only."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import text

from tests.contract.test_retrieval_classic_map_unit_contract import _publish_document
from tests.support.retrieval_snapshot_support import contract_db_session


async def _publish(namespace: str, chunk_id: str) -> str:
    published = await _publish_document(
        namespace=namespace,
        source_file_name="usage.pdf",
        chunks=[
            {
                "chunk_id": chunk_id,
                "type": "text",
                "content": "usage evidence",
                "path": "usage.pdf/Root/body",
                "order": 1,
            }
        ],
    )
    return published["document_id"]


async def _hit_counts(namespace: str) -> dict[tuple[str, str | None], int]:
    async with contract_db_session() as db:
        rows = await db.execute(
            text(
                "SELECT hit_kind, chunk_id, hit_count FROM retrieval_hit_stats "
                "WHERE namespace = :namespace"
            ),
            {"namespace": namespace},
        )
        return {(kind, chunk): count for kind, chunk, count in rows.all()}


async def _move_last_use_to_yesterday(namespace: str) -> None:
    async with contract_db_session() as db:
        await db.execute(
            text(
                "UPDATE retrieval_hit_stats SET last_hit_at = last_hit_at - interval '1 day' "
                "WHERE namespace = :namespace"
            ),
            {"namespace": namespace},
        )
        await db.commit()


async def test_usage_records_owned_documents_once_per_utc_day(
    developer_api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
) -> None:
    namespace = f"usage-{uuid4().hex[:8]}"
    chunk_id = f"{namespace}-body"
    async with developer_api_client_factory() as client:
        document_id = await _publish(namespace, chunk_id)
        body = {
            "namespace": namespace,
            "items": [
                {"document_id": document_id, "chunk_id": chunk_id},
                {"document_id": "doc_not_in_namespace"},
            ],
        }

        first = await client.post("/api/v2/retrieval/usage", json=body)
        assert first.status_code == 200 and first.json() == {"recorded": 1}
        assert await _hit_counts(namespace) == {("document", None): 1, ("chunk", chunk_id): 1}

        same_day = await client.post("/api/v2/retrieval/usage", json=body)
        assert same_day.status_code == 200
        assert await _hit_counts(namespace) == {("document", None): 1, ("chunk", chunk_id): 1}

        await _move_last_use_to_yesterday(namespace)
        next_day = await client.post(
            "/api/v2/retrieval/usage",
            json={"namespace": namespace, "items": [{"document_id": document_id}]},
        )
        assert next_day.status_code == 200 and next_day.json() == {"recorded": 1}
        assert await _hit_counts(namespace) == {("document", None): 2, ("chunk", chunk_id): 1}


async def test_usage_ignores_documents_from_another_namespace(
    developer_api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
) -> None:
    namespace = f"usage-{uuid4().hex[:8]}"
    other_namespace = f"usage-other-{uuid4().hex[:8]}"
    async with developer_api_client_factory() as client:
        document_id = await _publish(namespace, f"{namespace}-body")
        response = await client.post(
            "/api/v2/retrieval/usage",
            json={"namespace": other_namespace, "items": [{"document_id": document_id}]},
        )

    assert response.status_code == 200 and response.json() == {"recorded": 0}
    assert await _hit_counts(other_namespace) == {}
    assert await _hit_counts(namespace) == {}


async def test_usage_requires_authentication(
    developer_api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
) -> None:
    async with developer_api_client_factory() as client:
        client.headers.pop("Authorization", None)
        response = await client.post(
            "/api/v2/retrieval/usage",
            json={"namespace": "default", "items": [{"document_id": "doc_any"}]},
        )

    assert response.status_code == 401
