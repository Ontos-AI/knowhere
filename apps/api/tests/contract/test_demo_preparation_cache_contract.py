"""HTTP and database contract for demo preparation-cache parity."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from tests.support.contract_database import ContractDatabase

_SOURCE_ID = "demo-tsla-q4-2025"


async def _publish(
    client: AsyncClient,
    *,
    namespace: str,
) -> str:
    response = await client.post(
        "/api/v1/demo/materializations",
        json={"namespace": namespace, "demo_source_ids": [_SOURCE_ID]},
    )
    assert response.status_code == 200
    return str(response.json()["sources"][0]["document_id"])


async def _snapshot(document_id: str) -> dict[str, list[dict[str, Any]]]:
    chunks = await ContractDatabase.fetch_all(
        """
        SELECT chunk_id, chunk_type, content, content_search_text,
               path_search_text, term_search_text, source_chunk_path,
               file_path, sort_order
        FROM document_chunks
        WHERE document_id = :document_id
        ORDER BY sort_order, chunk_id, id
        """,
        {"document_id": document_id},
    )
    units = await ContractDatabase.fetch_all(
        """
        SELECT s.section_path, m.unit_kind, m.path_token_count,
               m.content_token_count, m.has_image, m.has_table, m.sort_order
        FROM document_map_units AS m
        JOIN document_sections AS s ON s.section_id = m.section_id
        WHERE m.document_id = :document_id
        ORDER BY m.sort_order, s.section_path, m.unit_kind, m.id
        """,
        {"document_id": document_id},
    )
    tokens = await ContractDatabase.fetch_all(
        """
        SELECT s.section_path, m.unit_kind, m.sort_order,
               t.channel, t.token, t.token_hash, t.frequency
        FROM document_map_unit_tokens AS t
        JOIN document_map_units AS m ON m.id = t.map_unit_id
        JOIN document_sections AS s ON s.section_id = m.section_id
        WHERE m.document_id = :document_id
        ORDER BY m.sort_order, s.section_path, m.unit_kind, t.channel, t.token
        """,
        {"document_id": document_id},
    )
    indexes = await ContractDatabase.fetch_all(
        """
        SELECT format_version, unit_count, token_count,
               average_idf_path, average_idf_content,
               path_document_count, path_total_length,
               content_document_count, content_total_length
        FROM document_map_unit_indexes
        WHERE document_id = :document_id
        """,
        {"document_id": document_id},
    )
    units = sorted(units, key=lambda row: tuple(str(value) for value in row.values()))
    tokens = sorted(
        tokens, key=lambda row: tuple(str(value) for value in row.values())
    )
    return {"chunks": chunks, "units": units, "tokens": tokens, "indexes": indexes}


@pytest.mark.asyncio
async def test_demo_preparation_cache_preserves_persisted_semantics_across_modes(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEMO_CANONICAL_BUNDLE_ENABLED", "true")
    monkeypatch.setenv("DEMO_PUBLICATION_PREPARATION_CACHE_ENABLED", "false")
    async with developer_api_client_factory() as client:
        from shared.services.retrieval.publication_preparation_cache import (
            PublicationPreparationCache,
        )

        PublicationPreparationCache.clear()
        cold_off_id = await _publish(client, namespace="prep-cache-off")
        off_snapshot = await _snapshot(cold_off_id)

        import shared.core.config as config_module

        monkeypatch.setattr(
            config_module.settings,
            "DEMO_PUBLICATION_PREPARATION_CACHE_ENABLED",
            True,
        )
        PublicationPreparationCache.clear()
        cache_counts = {"hits": 0, "misses": 0}
        original_get = PublicationPreparationCache._get

        def observe_get(key: object) -> object:
            value = original_get(key)  # type: ignore[arg-type]
            cache_counts["hits" if value is not None else "misses"] += 1
            return value

        monkeypatch.setattr(
            PublicationPreparationCache,
            "_get",
            classmethod(lambda _cls, key: observe_get(key)),
        )
        cold_on_id = await _publish(client, namespace="prep-cache-cold")
        cold_cache_hits = cache_counts["hits"]
        cold_cache_misses = cache_counts["misses"]
        warm_on_id = await _publish(client, namespace="prep-cache-warm")
        assert cold_cache_misses > 0
        assert cache_counts["hits"] > cold_cache_hits
        assert cache_counts["misses"] == cold_cache_misses
        assert len(PublicationPreparationCache._entries) > 0
        cold_snapshot = await _snapshot(cold_on_id)
        warm_snapshot = await _snapshot(warm_on_id)

        monkeypatch.setattr(
            config_module.settings,
            "DEMO_PUBLICATION_PREPARATION_CACHE_ENABLED",
            False,
        )
        rollback_id = await _publish(client, namespace="prep-cache-rollback")
        rollback_snapshot = await _snapshot(rollback_id)

        for namespace, expected_document_id in (
            ("prep-cache-off", cold_off_id),
            ("prep-cache-cold", cold_on_id),
            ("prep-cache-warm", warm_on_id),
            ("prep-cache-rollback", rollback_id),
        ):
            retrieval = await client.post(
                "/api/v1/retrieval/query",
                json={
                    "namespace": namespace,
                    "query": "Tesla investment",
                    "top_k": 3,
                    "use_agentic": False,
                },
            )
            assert retrieval.status_code == 200
            result_documents = {
                str(result["source"]["document_id"])
                for result in cast(list[dict[str, Any]], retrieval.json()["results"])
            }
            assert result_documents
            assert result_documents == {expected_document_id}

    assert cold_snapshot == off_snapshot
    assert warm_snapshot == off_snapshot
    assert rollback_snapshot == off_snapshot
