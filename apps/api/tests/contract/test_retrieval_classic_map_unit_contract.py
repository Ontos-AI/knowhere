from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, cast
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import select

from shared.models.database.document import DocumentMapUnit
from shared.services.retrieval.publication_content import (
    replace_document_revision_content,
)
from shared.services.retrieval.publication_models import DocumentPublicationScope
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
