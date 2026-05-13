from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from tests.support.contract_database import ContractDatabase


DEMO_SOURCE_ID = "demo-tsla-q4-2025"


class FakeResultStorage:
    def __init__(self) -> None:
        self.raw_files_by_job_id: dict[str, set[str]] = {}

    def upload(
        self,
        *,
        job_id: str,
        result_dir: str,
        zip_file_path: str,
    ) -> SimpleNamespace:
        assert Path(zip_file_path).is_file()
        result_path = Path(result_dir)
        raw_files = {
            file_path.relative_to(result_path).as_posix()
            for file_path in result_path.rglob("*")
            if file_path.is_file()
        }
        self.raw_files_by_job_id[job_id] = raw_files
        return SimpleNamespace(
            zip_key=f"results/{job_id}.zip",
            raw_prefix=f"results/{job_id}/",
            raw_files={
                raw_file: f"results/{job_id}/{raw_file}"
                for raw_file in sorted(raw_files)
            },
        )

    def normalize_artifact_ref(self, artifact_ref: str | None) -> str | None:
        if not artifact_ref:
            return None
        normalized = str(artifact_ref).strip().replace("\\", "/").lstrip("/")
        if normalized.startswith("images/") or normalized.startswith("tables/"):
            return normalized
        return None

    def generate_artifact_url(
        self,
        *,
        job_id: str,
        artifact_ref: str,
        expires_in: int = 3600,
    ) -> str | None:
        normalized = self.normalize_artifact_ref(artifact_ref)
        if not normalized:
            return None
        if normalized not in self.raw_files_by_job_id.get(job_id, set()):
            return None
        return f"https://assets.example.test/{job_id}/{normalized}"


@pytest.mark.asyncio
async def test_should_return_demo_catalog_with_resolvable_canonical_citations(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
) -> None:
    async with api_client_factory() as api_client:
        catalog_response = await api_client.get("/api/v1/demo/catalog")

    assert catalog_response.status_code == 200
    catalog = cast(dict[str, Any], catalog_response.json())
    sources = cast(list[dict[str, Any]], catalog["sources"])
    source = sources[0]
    examples = cast(list[dict[str, Any]], source["examples"])
    citations = cast(list[dict[str, Any]], examples[0]["citations"])
    citation = citations[0]

    assert source["demo_source_id"] == DEMO_SOURCE_ID
    assert source["canonical_document_id"] == "demo-doc-tsla-q4-2025"
    assert source["chunk_count"] == 70
    assert source["original_file"]["can_download"] is False
    assert citation["canonical_document_id"] == "demo-doc-tsla-q4-2025"
    assert citation["canonical_chunk_id"].startswith(f"{DEMO_SOURCE_ID}:")

    async with api_client_factory() as api_client:
        chunks_response = await api_client.get(
            f"/api/v1/demo/sources/{DEMO_SOURCE_ID}/chunks?page_size=200"
        )
        chunk_response = await api_client.get(
            "/api/v1/demo/sources/"
            f"{DEMO_SOURCE_ID}/chunks/{citation['canonical_chunk_id']}"
        )

    assert chunks_response.status_code == 200
    assert chunk_response.status_code == 200
    chunks_body = cast(dict[str, Any], chunks_response.json())
    chunk_page = cast(list[dict[str, Any]], chunks_body["chunks"])
    asset_url = next(
        str(chunk["asset_url"]) for chunk in chunk_page if chunk.get("asset_url")
    )
    chunk_body = cast(dict[str, Any], chunk_response.json())
    chunk = cast(dict[str, Any], chunk_body["chunk"])

    assert chunk["id"] == citation["canonical_chunk_id"]
    assert citation["content"] in chunk["content"]

    async with api_client_factory() as api_client:
        asset_response = await api_client.get(asset_url)
        original_response = await api_client.get(
            f"/api/v1/demo/sources/{DEMO_SOURCE_ID}/original"
        )
        internal_asset_response = await api_client.get(
            f"/api/v1/demo/sources/{DEMO_SOURCE_ID}/assets/full.md"
        )

    assert asset_response.status_code == 200
    assert asset_response.headers["content-disposition"].startswith("inline")
    assert original_response.status_code == 200
    assert original_response.headers["content-disposition"].startswith("inline")
    assert internal_asset_response.status_code == 404


@pytest.mark.asyncio
async def test_should_materialize_demo_source_without_parse_or_credit_charge(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    fake_result_storage = FakeResultStorage()
    monkeypatch.setattr(
        "shared.services.storage.result_storage.get_result_storage",
        lambda: fake_result_storage,
    )
    monkeypatch.setenv("RETRIEVAL_AGENTIC_ENABLED", "false")

    async with developer_api_client_factory() as api_client:
        empty_cached_response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-demo",
                "query": "xAI investment",
                "top_k": 5,
            },
        )
        first_response = await api_client.post(
            "/api/v1/demo/materializations",
            json={
                "namespace": "contract-demo",
                "demo_source_ids": [DEMO_SOURCE_ID],
            },
        )
        retry_response = await api_client.post(
            "/api/v1/demo/materializations",
            json={
                "namespace": "contract-demo",
                "demo_source_ids": [DEMO_SOURCE_ID],
            },
        )
        retrieval_response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-demo",
                "query": "xAI investment",
                "top_k": 5,
            },
        )
        document_chunks_response = await api_client.get(
            f"/api/v1/documents/{first_response.json()['sources'][0]['document_id']}"
            "/chunks?page_size=200"
        )

    assert empty_cached_response.status_code == 200
    assert first_response.status_code == 200
    assert retry_response.status_code == 200
    assert retrieval_response.status_code == 200
    assert document_chunks_response.status_code == 200

    empty_cached_body = cast(dict[str, Any], empty_cached_response.json())
    assert cast(list[dict[str, Any]], empty_cached_body["results"]) == []

    first_source = cast(dict[str, Any], first_response.json()["sources"][0])
    retry_source = cast(dict[str, Any], retry_response.json()["sources"][0])
    document_id = str(first_source["document_id"])

    assert first_source["status"] == "created"
    assert retry_source["status"] == "existing"
    assert retry_source["document_id"] == document_id

    materialization_rows = await ContractDatabase.fetch_all(
        """
        SELECT demo_source_id, document_id
        FROM demo_materializations
        WHERE user_id = 'local-dev-user'
          AND namespace = 'contract-demo'
          AND demo_source_id = :demo_source_id
        """,
        {"demo_source_id": DEMO_SOURCE_ID},
    )
    document_row = await ContractDatabase.fetch_one(
        """
        SELECT document_id, status, source_file_name
        FROM documents
        WHERE document_id = :document_id
        """,
        {"document_id": document_id},
    )
    chunk_rows = await ContractDatabase.fetch_all(
        """
        SELECT id
        FROM document_chunks
        WHERE document_id = :document_id
        """,
        {"document_id": document_id},
    )
    job_rows = await ContractDatabase.fetch_all(
        """
        SELECT job_id, status, job_type, credits_charged, billing_status
        FROM jobs
        WHERE user_id = 'local-dev-user'
          AND job_metadata ->> 'demo_source_id' = :demo_source_id
        """,
        {"demo_source_id": DEMO_SOURCE_ID},
    )

    assert materialization_rows == [
        {"demo_source_id": DEMO_SOURCE_ID, "document_id": document_id}
    ]
    assert document_row == {
        "document_id": document_id,
        "status": "active",
        "source_file_name": "TSLA-Q4-2025-Update.pdf",
    }
    assert len(chunk_rows) == 70
    assert len(job_rows) == 1
    job_row = job_rows[0]
    assert job_row["status"] == "done"
    assert job_row["job_type"] == "demo_materialization"
    assert job_row["credits_charged"] == 0
    assert job_row["billing_status"] == "skipped"

    retrieval_body = cast(dict[str, Any], retrieval_response.json())
    retrieval_results = cast(list[dict[str, Any]], retrieval_body["results"])
    chunk_page_body = cast(dict[str, Any], document_chunks_response.json())
    materialized_chunks = cast(list[dict[str, Any]], chunk_page_body["chunks"])
    media_chunks = [
        chunk
        for chunk in materialized_chunks
        if chunk["chunk_type"] in {"image", "table"}
    ]

    assert retrieval_body["namespace"] == "contract-demo"
    assert retrieval_results
    assert retrieval_results[0]["source"]["document_id"] == document_id
    assert retrieval_results[0]["source"]["section_path"] != "Root"
    assert media_chunks
    assert media_chunks[0]["file_path"]
    uploaded_files = fake_result_storage.raw_files_by_job_id[str(job_row["job_id"])]
    assert any(file_path.startswith("images/") for file_path in uploaded_files)
    assert any(file_path.startswith("tables/") for file_path in uploaded_files)


@pytest.mark.asyncio
async def test_should_serialize_concurrent_first_demo_materialization(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    fake_result_storage = FakeResultStorage()
    monkeypatch.setattr(
        "shared.services.storage.result_storage.get_result_storage",
        lambda: fake_result_storage,
    )

    async with developer_api_client_factory() as api_client:
        first_response, second_response = await asyncio.gather(
            api_client.post(
                "/api/v1/demo/materializations",
                json={
                    "namespace": "contract-demo-race",
                    "demo_source_ids": [DEMO_SOURCE_ID],
                },
            ),
            api_client.post(
                "/api/v1/demo/materializations",
                json={
                    "namespace": "contract-demo-race",
                    "demo_source_ids": [DEMO_SOURCE_ID],
                },
            ),
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200

    first_source = cast(dict[str, Any], first_response.json()["sources"][0])
    second_source = cast(dict[str, Any], second_response.json()["sources"][0])
    document_ids = {
        str(first_source["document_id"]),
        str(second_source["document_id"]),
    }
    statuses = {str(first_source["status"]), str(second_source["status"])}

    assert len(document_ids) == 1
    assert statuses == {"created", "existing"}

    materialization_rows = await ContractDatabase.fetch_all(
        """
        SELECT demo_source_id, document_id
        FROM demo_materializations
        WHERE user_id = 'local-dev-user'
          AND namespace = 'contract-demo-race'
          AND demo_source_id = :demo_source_id
        """,
        {"demo_source_id": DEMO_SOURCE_ID},
    )
    job_rows = await ContractDatabase.fetch_all(
        """
        SELECT job_id
        FROM jobs
        WHERE user_id = 'local-dev-user'
          AND job_metadata ->> 'namespace' = 'contract-demo-race'
          AND job_metadata ->> 'demo_source_id' = :demo_source_id
        """,
        {"demo_source_id": DEMO_SOURCE_ID},
    )

    assert materialization_rows == [
        {
            "demo_source_id": DEMO_SOURCE_ID,
            "document_id": next(iter(document_ids)),
        }
    ]
    assert len(job_rows) == 1


@pytest.mark.asyncio
async def test_should_reject_blank_demo_materialization_selection(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        response = await api_client.post(
            "/api/v1/demo/materializations",
            json={
                "namespace": "contract-demo-blank",
                "demo_source_ids": [" ", "\t"],
            },
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_should_reject_mixed_demo_materialization_selection_before_upload(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    fake_result_storage = FakeResultStorage()
    monkeypatch.setattr(
        "shared.services.storage.result_storage.get_result_storage",
        lambda: fake_result_storage,
    )

    async with developer_api_client_factory() as api_client:
        response = await api_client.post(
            "/api/v1/demo/materializations",
            json={
                "namespace": "contract-demo-mixed-invalid",
                "demo_source_ids": [DEMO_SOURCE_ID, "missing-demo-source"],
            },
        )

    job_rows = await ContractDatabase.fetch_all(
        """
        SELECT job_id
        FROM jobs
        WHERE user_id = 'local-dev-user'
          AND job_metadata ->> 'namespace' = 'contract-demo-mixed-invalid'
        """,
    )
    materialization_rows = await ContractDatabase.fetch_all(
        """
        SELECT demo_source_id, document_id
        FROM demo_materializations
        WHERE user_id = 'local-dev-user'
          AND namespace = 'contract-demo-mixed-invalid'
        """,
    )

    assert response.status_code == 404
    assert fake_result_storage.raw_files_by_job_id == {}
    assert job_rows == []
    assert materialization_rows == []
