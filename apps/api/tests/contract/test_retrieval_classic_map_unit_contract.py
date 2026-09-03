from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, cast
from uuid import uuid4

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import Engine, event, select

from shared.models.database.document import DocumentMapUnit
from shared.services.retrieval.publication_content import (
    replace_document_revision_content,
)
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.search.map_unit_discovery import (
    DiscoveryResult,
    map_unit_discovery,
)
from shared.services.retrieval.serving_generation import lock_namespace_generation
from tests.support.contract_database import ContractDatabase
from tests.support.retrieval_snapshot_support import contract_db_session

_USER_ID = "local-dev-user"


async def test_classic_route_maps_winning_unit_to_one_chunk(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    identifier = uuid4().hex[:8]
    namespace = f"classic-map-{identifier}"
    async with developer_api_client_factory() as api_client:
        first = await _publish_document(
            namespace=namespace,
            source_file_name="first.pdf",
            chunks=[
                {
                    "chunk_id": f"hit-{identifier}",
                    "type": "text",
                    "content": "unique alpha ranking marker",
                    "path": "first.pdf/Root/Hit/body",
                    "order": 1,
                    "metadata": {},
                },
                {
                    "chunk_id": f"other-{identifier}",
                    "type": "text",
                    "content": "unrelated filler paragraph",
                    "path": "first.pdf/Root/Other/body",
                    "order": 2,
                    "metadata": {},
                },
            ],
        )
        await _publish_document(
            namespace=namespace,
            source_file_name="second.pdf",
            chunks=[
                {
                    "chunk_id": f"noise-{identifier}",
                    "type": "text",
                    "content": "more unrelated filler",
                    "path": "second.pdf/Root/Noise/body",
                    "order": 1,
                    "metadata": {},
                },
            ],
        )
        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": namespace,
                "query": "unique alpha ranking",
                "top_k": 1,
                "use_agentic": False,
            },
        )

    assert response.status_code == 200
    body = cast(dict[str, object], response.json())
    results = cast(list[dict[str, object]], body["results"])
    assert body["router_used"] == "classic_topk"
    assert len(results) == 1
    assert results[0]["chunk_id"] == f"hit-{identifier}"
    assert results[0]["chunk_type"] == "text"
    assert results[0]["source"] == {
        "document_id": first["document_id"],
        "source_file_name": "first.pdf",
        "section_path": "Root / Hit / body",
    }


async def test_classic_route_uses_token_hash_lookup_for_frequency_query(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    identifier = uuid4().hex[:8]
    namespace = f"classic-token-hash-{identifier}"
    statements: list[str] = []

    def capture_frequency_query(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            "FROM document_map_unit_tokens" in statement
            and "frequency" in statement
            and "token_hash" in statement
        ):
            statements.append(statement)

    event.listen(Engine, "before_cursor_execute", capture_frequency_query)
    try:
        async with developer_api_client_factory() as api_client:
            await _publish_document(
                namespace=namespace,
                source_file_name="token-hash.pdf",
                chunks=[
                    {
                        "chunk_id": f"token-hash-{identifier}",
                        "type": "text",
                        "content": "token hash lookup marker",
                        "path": "token-hash.pdf/Root/Section/body",
                        "order": 1,
                        "metadata": {},
                    },
                    {
                        "chunk_id": f"token-hash-filler-a-{identifier}",
                        "type": "text",
                        "content": "unrelated filler a",
                        "path": "token-hash.pdf/Root/Section/a",
                        "order": 2,
                        "metadata": {},
                    },
                    {
                        "chunk_id": f"token-hash-filler-b-{identifier}",
                        "type": "text",
                        "content": "unrelated filler b",
                        "path": "token-hash.pdf/Root/Section/b",
                        "order": 3,
                        "metadata": {},
                    },
                    {
                        "chunk_id": f"token-hash-filler-c-{identifier}",
                        "type": "text",
                        "content": "unrelated filler c",
                        "path": "token-hash.pdf/Root/Section/c",
                        "order": 4,
                        "metadata": {},
                    },
                ],
            )
            response = await api_client.post(
                "/api/v1/retrieval/query",
                json={
                    "namespace": namespace,
                    "query": "token hash lookup",
                    "top_k": 1,
                    "use_agentic": False,
                },
            )
    finally:
        event.remove(Engine, "before_cursor_execute", capture_frequency_query)

    assert response.status_code == 200
    assert statements
    assert "token_hash = ANY" in statements[-1]
    assert "token = ANY" not in statements[-1]
    assert "matching_tokens AS MATERIALIZED" in statements[-1]
    assert "FROM matching_tokens" in statements[-1]


async def test_classic_route_only_uses_token_selective_projection_for_unfiltered_scope(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    identifier = uuid4().hex[:8]
    namespace = f"classic-projection-{identifier}"
    projection_statements: list[str] = []

    def capture_unit_projection(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "SELECT DISTINCT scoped_units.*" in statement:
            projection_statements.append("token_selective")
        elif "SELECT * FROM scoped_units" in statement:
            projection_statements.append("legacy")

    event.listen(Engine, "before_cursor_execute", capture_unit_projection)
    try:
        async with developer_api_client_factory() as api_client:
            await _publish_document(
                namespace=namespace,
                source_file_name="projection.pdf",
                chunks=[
                    {
                        "chunk_id": f"projection-hit-{identifier}",
                        "type": "text",
                        "content": "projection token marker",
                        "path": "projection.pdf/Root/Section/hit",
                        "order": 1,
                        "metadata": {},
                    },
                    {
                        "chunk_id": f"projection-filler-a-{identifier}",
                        "type": "text",
                        "content": "unrelated filler a",
                        "path": "projection.pdf/Root/Section/a",
                        "order": 2,
                        "metadata": {},
                    },
                    {
                        "chunk_id": f"projection-filler-b-{identifier}",
                        "type": "text",
                        "content": "unrelated filler b",
                        "path": "projection.pdf/Root/Section/b",
                        "order": 3,
                        "metadata": {},
                    },
                    {
                        "chunk_id": f"projection-filler-c-{identifier}",
                        "type": "text",
                        "content": "unrelated filler c",
                        "path": "projection.pdf/Root/Section/c",
                        "order": 4,
                        "metadata": {},
                    },
                ],
            )
            unfiltered_response = await api_client.post(
                "/api/v1/retrieval/query",
                json={
                    "namespace": namespace,
                    "query": "projection token marker",
                    "top_k": 1,
                    "use_agentic": False,
                },
            )
            filtered_response = await api_client.post(
                "/api/v1/retrieval/query",
                json={
                    "namespace": namespace,
                    "query": "projection token marker",
                    "top_k": 1,
                    "use_agentic": False,
                    "signal_paths": ["Section"],
                    "filter_mode": "keep",
                },
            )
    finally:
        event.remove(Engine, "before_cursor_execute", capture_unit_projection)

    assert unfiltered_response.status_code == 200
    assert filtered_response.status_code == 200
    assert projection_statements[:2] == ["token_selective", "legacy"]


async def test_unfiltered_revision_pins_reuse_index_metadata_without_scoped_revision_cte(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    identifier = uuid4().hex[:8]
    namespace = f"classic-pinned-index-{identifier}"
    index_statements: list[str] = []

    def capture_index_query(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            "document_map_unit_indexes" in statement
            and "average_idf_path" in statement
        ):
            index_statements.append(statement)

    async with developer_api_client_factory():
        document = await _publish_document(
            namespace=namespace,
            source_file_name="pinned-index.pdf",
            chunks=[
                {
                    "chunk_id": f"pinned-hit-{identifier}",
                    "type": "text",
                    "content": "pinned revision semantic marker",
                    "path": "pinned-index.pdf/Root/Section/hit",
                    "order": 1,
                    "metadata": {},
                },
                {
                    "chunk_id": f"pinned-filler-{identifier}",
                    "type": "text",
                    "content": "unrelated filler",
                    "path": "pinned-index.pdf/Root/Section/filler",
                    "order": 2,
                    "metadata": {},
                },
            ],
        )

        def result_signature(result: Any) -> list[tuple[Any, ...]]:
            rows = list(result.payload.get("fused_rows") or [])
            return [
                (
                    row.get("chunk_id"),
                    row.get("document_id"),
                    row.get("job_result_id"),
                    row.get("section_path"),
                    row.get("score"),
                    row.get("discovery_score"),
                    row.get("content"),
                )
                for row in rows
            ]

        event.listen(Engine, "before_cursor_execute", capture_index_query)
        try:
            async with contract_db_session() as db:
                unpinned = await map_unit_discovery(
                    db,
                    user_id=_USER_ID,
                    namespace=namespace,
                    query="pinned revision semantic marker",
                    top_k=10,
                    exclude_document_ids=[],
                    exclude_sections=[],
                    revision_pins=None,
                )

            index_statements.clear()

            async with contract_db_session() as db:
                pinned = await map_unit_discovery(
                    db,
                    user_id=_USER_ID,
                    namespace=namespace,
                    query="pinned revision semantic marker",
                    top_k=10,
                    exclude_document_ids=[],
                    exclude_sections=[],
                    revision_pins={
                        document["document_id"]: document["job_result_id"]
                    },
                )
        finally:
            event.remove(Engine, "before_cursor_execute", capture_index_query)

    assert result_signature(pinned) == result_signature(unpinned)
    assert index_statements
    assert all("JOIN (VALUES" in statement for statement in index_statements)
    assert all("scoped_units AS" not in statement for statement in index_statements)
    assert all(
        "SELECT DISTINCT document_id, job_result_id" not in statement
        for statement in index_statements
    )


async def test_classic_discovery_returns_empty_for_an_empty_revision_pin(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    namespace: str = f"classic-empty-pins-{uuid4().hex[:8]}"
    async with developer_api_client_factory():
        async with contract_db_session() as db:
            result: DiscoveryResult = await map_unit_discovery(
                db,
                user_id=_USER_ID,
                namespace=namespace,
                query="empty revision marker",
                top_k=1,
                exclude_document_ids=[],
                exclude_sections=[],
                revision_pins={},
            )

    assert result.payload["fused_rows"] == []


async def test_classic_route_falls_back_for_v1_index_with_excluded_document(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    identifier = uuid4().hex[:8]
    namespace = f"classic-v1-fallback-{identifier}"
    legacy_queries: list[str] = []

    def capture_legacy_query(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "plainto_tsquery('simple'" in statement:
            legacy_queries.append(statement)

    event.listen(Engine, "before_cursor_execute", capture_legacy_query)
    try:
        async with developer_api_client_factory() as api_client:
            first = await _publish_document(
                namespace=namespace,
                source_file_name="legacy-fallback.pdf",
                chunks=[
                    {
                        "chunk_id": f"legacy-hit-{identifier}",
                        "type": "text",
                        "content": "legacy fallback marker",
                        "path": "legacy-fallback.pdf/Root/Section/body",
                        "order": 1,
                        "metadata": {},
                    },
                    {
                        "chunk_id": f"legacy-filler-a-{identifier}",
                        "type": "text",
                        "content": "unrelated legacy filler a",
                        "path": "legacy-fallback.pdf/Root/Section/a",
                        "order": 2,
                        "metadata": {},
                    },
                    {
                        "chunk_id": f"legacy-filler-b-{identifier}",
                        "type": "text",
                        "content": "unrelated legacy filler b",
                        "path": "legacy-fallback.pdf/Root/Section/b",
                        "order": 3,
                        "metadata": {},
                    },
                ],
            )
            excluded = await _publish_document(
                namespace=namespace,
                source_file_name="excluded.pdf",
                chunks=[
                    {
                        "chunk_id": f"excluded-{identifier}",
                        "type": "text",
                        "content": "unrelated filler",
                        "path": "excluded.pdf/Root/Section/body",
                        "order": 1,
                        "metadata": {},
                    }
                ],
            )
            await ContractDatabase.execute(
                """
                UPDATE document_map_unit_indexes
                SET format_version = 1
                WHERE document_id = :document_id
                """,
                {"document_id": first["document_id"]},
            )
            response = await api_client.post(
                "/api/v1/retrieval/query",
                json={
                    "namespace": namespace,
                    "query": "legacy fallback marker",
                    "top_k": 1,
                    "use_agentic": False,
                    "exclude_document_ids": [excluded["document_id"]],
                },
            )
    finally:
        event.remove(Engine, "before_cursor_execute", capture_legacy_query)

    assert response.status_code == 200
    body = cast(dict[str, object], response.json())
    results = cast(list[dict[str, object]], body["results"])
    assert len(results) == 1
    assert results[0]["chunk_id"] == f"legacy-hit-{identifier}"
    assert legacy_queries


async def test_classic_discovery_preserves_results_before_statistics_backfill(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    identifier = uuid4().hex[:8]
    namespace = f"classic-statistics-parity-{identifier}"
    legacy_queries: list[str] = []

    def capture_legacy_query(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "plainto_tsquery('simple'" in statement:
            legacy_queries.append(statement)

    def result_signature(result: DiscoveryResult) -> list[tuple[Any, ...]]:
        return [
            (
                row.get("chunk_id"),
                row.get("document_id"),
                row.get("job_result_id"),
                row.get("section_path"),
                row.get("source_file_name"),
                row.get("score"),
                row.get("discovery_score"),
                row.get("content"),
                row.get("chunk_metadata"),
            )
            for row in list(result.payload.get("fused_rows") or [])
        ]

    event.listen(Engine, "before_cursor_execute", capture_legacy_query)
    try:
        async with developer_api_client_factory():
            document = await _publish_document(
                namespace=namespace,
                source_file_name="statistics-parity.pdf",
                chunks=[
                    {
                        "chunk_id": f"statistics-hit-{identifier}",
                        "type": "text",
                        "content": "statistics parity retrieval marker",
                        "path": "statistics-parity.pdf/Root/Section/hit",
                        "order": 1,
                        "metadata": {},
                    },
                    {
                        "chunk_id": f"statistics-filler-a-{identifier}",
                        "type": "text",
                        "content": "unrelated statistics filler a",
                        "path": "statistics-parity.pdf/Root/Section/a",
                        "order": 2,
                        "metadata": {},
                    },
                    {
                        "chunk_id": f"statistics-filler-b-{identifier}",
                        "type": "text",
                        "content": "unrelated statistics filler b",
                        "path": "statistics-parity.pdf/Root/Section/b",
                        "order": 3,
                        "metadata": {},
                    },
                ],
            )
            query = "statistics parity retrieval marker"
            async with contract_db_session() as db:
                post_backfill = await map_unit_discovery(
                    db,
                    user_id=_USER_ID,
                    namespace=namespace,
                    query=query,
                    top_k=3,
                    exclude_document_ids=[],
                    exclude_sections=[],
                )

            await ContractDatabase.execute(
                """
                UPDATE document_map_unit_indexes
                SET path_document_count = NULL,
                    path_total_length = NULL,
                    content_document_count = NULL,
                    content_total_length = NULL
                WHERE document_id = :document_id
                """,
                {"document_id": document["document_id"]},
            )

            async with contract_db_session() as db:
                pre_backfill = await map_unit_discovery(
                    db,
                    user_id=_USER_ID,
                    namespace=namespace,
                    query=query,
                    top_k=3,
                    exclude_document_ids=[],
                    exclude_sections=[],
                )
    finally:
        event.remove(Engine, "before_cursor_execute", capture_legacy_query)

    assert result_signature(pre_backfill) == result_signature(post_backfill)
    assert legacy_queries == []


@pytest.mark.parametrize(
    "incomplete_index_kind", ["legacy_format", "missing_index", "missing_tokens"]
)
async def test_unfiltered_classic_route_falls_back_when_selective_rows_are_unavailable(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    incomplete_index_kind: str,
) -> None:
    identifier: str = uuid4().hex[:8]
    namespace: str = f"classic-token-fallback-{incomplete_index_kind}-{identifier}"
    legacy_queries: list[str] = []

    def capture_legacy_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "plainto_tsquery('simple'" in statement:
            legacy_queries.append(statement)

    event.listen(Engine, "before_cursor_execute", capture_legacy_query)
    try:
        async with developer_api_client_factory() as api_client:
            document: dict[str, str] = await _publish_document(
                namespace=namespace,
                source_file_name="legacy-token-hash.pdf",
                chunks=[
                    {
                        "chunk_id": f"legacy-token-hit-{identifier}",
                        "type": "text",
                        "content": "legacy token fallback marker",
                        "path": "legacy-token-hash.pdf/Root/Section/body",
                        "order": 1,
                        "metadata": {},
                    },
                    {
                        "chunk_id": f"legacy-token-filler-a-{identifier}",
                        "type": "text",
                        "content": "unrelated legacy filler a",
                        "path": "legacy-token-hash.pdf/Root/Section/a",
                        "order": 2,
                        "metadata": {},
                    },
                    {
                        "chunk_id": f"legacy-token-filler-b-{identifier}",
                        "type": "text",
                        "content": "unrelated legacy filler b",
                        "path": "legacy-token-hash.pdf/Root/Section/b",
                        "order": 3,
                        "metadata": {},
                    },
                ],
            )
            if incomplete_index_kind == "legacy_format":
                await ContractDatabase.execute(
                    """
                    UPDATE document_map_unit_indexes
                    SET format_version = 1
                    WHERE document_id = :document_id
                    """,
                    {"document_id": document["document_id"]},
                )
                await ContractDatabase.execute(
                    """
                    UPDATE document_map_unit_tokens
                    SET token_hash = :legacy_token_hash
                    WHERE map_unit_id IN (
                        SELECT id
                        FROM document_map_units
                        WHERE document_id = :document_id
                    )
                    """,
                    {
                        "document_id": document["document_id"],
                        "legacy_token_hash": "legacy-token-hash",
                    },
                )
            elif incomplete_index_kind == "missing_tokens":
                await ContractDatabase.execute(
                    """
                    DELETE FROM document_map_unit_tokens
                    WHERE map_unit_id IN (
                        SELECT id
                        FROM document_map_units
                        WHERE document_id = :document_id
                    )
                    """,
                    {"document_id": document["document_id"]},
                )
            else:
                await ContractDatabase.execute(
                    """
                    DELETE FROM document_map_unit_indexes
                    WHERE document_id = :document_id
                    """,
                    {"document_id": document["document_id"]},
                )
                await ContractDatabase.execute(
                    """
                    DELETE FROM document_map_unit_tokens
                    WHERE map_unit_id IN (
                        SELECT id
                        FROM document_map_units
                        WHERE document_id = :document_id
                    )
                    """,
                    {"document_id": document["document_id"]},
                )
            response: Response = await api_client.post(
                "/api/v1/retrieval/query",
                json={
                    "namespace": namespace,
                    "query": "legacy token fallback marker",
                    "top_k": 1,
                    "use_agentic": False,
                },
            )
    finally:
        event.remove(Engine, "before_cursor_execute", capture_legacy_query)

    assert response.status_code == 200
    body = cast(dict[str, object], response.json())
    results = cast(list[dict[str, object]], body["results"])
    assert len(results) == 1
    assert results[0]["chunk_id"] == f"legacy-token-hit-{identifier}"
    assert legacy_queries


async def test_classic_route_image_filter_scores_only_units_with_images(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    identifier = uuid4().hex[:8]
    namespace = f"classic-image-{identifier}"
    async with developer_api_client_factory() as api_client:
        with_image = await _publish_document(
            namespace=namespace,
            source_file_name="with-image.pdf",
            chunks=[
                {
                    "chunk_id": f"body-{identifier}",
                    "type": "text",
                    "content": "shared alpha marker next to a chart",
                    "path": "with-image.pdf/Root/Section/body",
                    "order": 1,
                    "metadata": {"connect_to": [{"target": f"chart-{identifier}"}]},
                },
                {
                    "chunk_id": f"chart-{identifier}",
                    "type": "image",
                    "content": "chart of shared alpha marker",
                    "path": "images/chart.png",
                    "order": 2,
                    "file_path": "images/chart.png",
                    "metadata": {},
                },
            ],
        )
        await _publish_document(
            namespace=namespace,
            source_file_name="text-only.pdf",
            chunks=[
                {
                    "chunk_id": f"text-only-{identifier}",
                    "type": "text",
                    "content": "shared alpha marker with no chart",
                    "path": "text-only.pdf/Root/Section/body",
                    "order": 1,
                    "metadata": {},
                },
            ],
        )
        await _publish_document(
            namespace=namespace,
            source_file_name="other-image.pdf",
            chunks=[
                {
                    "chunk_id": f"other-body-{identifier}",
                    "type": "text",
                    "content": "unrelated filler next to a photo",
                    "path": "other-image.pdf/Root/Section/body",
                    "order": 1,
                    "metadata": {"connect_to": [{"target": f"photo-{identifier}"}]},
                },
                {
                    "chunk_id": f"photo-{identifier}",
                    "type": "image",
                    "content": "unrelated landscape photo",
                    "path": "images/photo.png",
                    "order": 2,
                    "file_path": "images/photo.png",
                    "metadata": {},
                },
            ],
        )
        await _publish_document(
            namespace=namespace,
            source_file_name="third-image.pdf",
            chunks=[
                {
                    "chunk_id": f"third-body-{identifier}",
                    "type": "text",
                    "content": "another unrelated caption beside a diagram",
                    "path": "third-image.pdf/Root/Section/body",
                    "order": 1,
                    "metadata": {"connect_to": [{"target": f"diagram-{identifier}"}]},
                },
                {
                    "chunk_id": f"diagram-{identifier}",
                    "type": "image",
                    "content": "unrelated diagram",
                    "path": "images/diagram.png",
                    "order": 2,
                    "file_path": "images/diagram.png",
                    "metadata": {},
                },
            ],
        )
        async with contract_db_session() as db:
            image_units = list(
                (
                    await db.execute(
                        select(DocumentMapUnit).where(
                            DocumentMapUnit.document_id == with_image["document_id"]
                        )
                    )
                ).scalars()
            )
        assert any(unit.has_image for unit in image_units)

        response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": namespace,
                "query": "shared alpha marker",
                "top_k": 1,
                "use_agentic": False,
                "chunk_types": ["image"],
            },
        )

    assert response.status_code == 200
    body = cast(dict[str, object], response.json())
    results = cast(list[dict[str, object]], body["results"])
    assert body["router_used"] == "classic_topk"
    assert len(results) == 1
    assert results[0]["chunk_id"] == f"chart-{identifier}"
    assert results[0]["chunk_type"] == "image"


def _publish_revision_with_generation_lock(
    sync_db: Any,
    *,
    scope: DocumentPublicationScope,
    chunks: list[dict[str, Any]],
) -> None:
    lock_namespace_generation(
        sync_db, user_id=scope.user_id, namespace=scope.namespace
    )
    replace_document_revision_content(sync_db, scope=scope, chunks=chunks)


async def _publish_document(
    *,
    namespace: str,
    source_file_name: str,
    chunks: list[dict[str, Any]],
) -> dict[str, str]:
    document_id = f"doc_{uuid4().hex[:12]}"
    job_id = f"job_{uuid4().hex[:12]}"
    job_result_id = f"result_{uuid4().hex[:12]}"
    await ContractDatabase.execute(
        """
        INSERT INTO jobs (
            job_id, user_id, job_type, status, source_type, version,
            webhook_enabled, created_at, updated_at, credits_charged, billing_status
        ) VALUES (
            :job_id, :user_id, 'document_ingestion', 'done', 'file', 0,
            false, NOW(), NOW(), 0, 'skipped'
        )
        """,
        {"job_id": job_id, "user_id": _USER_ID},
    )
    await ContractDatabase.execute(
        """
        INSERT INTO documents (
            document_id, user_id, namespace, status, source_file_name,
            parse_track, created_at, updated_at
        ) VALUES (
            :document_id, :user_id, :namespace, 'active',
            :source_file_name, 'chunk', NOW(), NOW()
        )
        """,
        {
            "document_id": document_id,
            "user_id": _USER_ID,
            "namespace": namespace,
            "source_file_name": source_file_name,
        },
    )
    await ContractDatabase.execute(
        """
        INSERT INTO job_results (
            id, job_id, document_id, delivery_mode, created_at, updated_at
        ) VALUES (
            :job_result_id, :job_id, :document_id, 'inline', NOW(), NOW()
        )
        """,
        {
            "job_result_id": job_result_id,
            "job_id": job_id,
            "document_id": document_id,
        },
    )
    await ContractDatabase.execute(
        """
        UPDATE documents SET current_job_result_id = :job_result_id
        WHERE document_id = :document_id
        """,
        {"job_result_id": job_result_id, "document_id": document_id},
    )
    scope = DocumentPublicationScope(
        user_id=_USER_ID,
        namespace=namespace,
        document_id=document_id,
        job_result_id=job_result_id,
        source_file_name=source_file_name,
    )
    async with contract_db_session() as db:
        await db.run_sync(
            lambda sync_db: _publish_revision_with_generation_lock(
                sync_db,
                scope=scope,
                chunks=chunks,
            )
        )
        await db.commit()
    return {
        "document_id": document_id,
        "job_id": job_id,
        "job_result_id": job_result_id,
    }
