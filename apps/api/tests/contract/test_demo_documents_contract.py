from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType
from typing import Any, cast

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from tests.support.import_environment import (
    configure_import_environment,
    ensure_import_paths,
)
from tests.support.contract_database import ContractDatabase


DEMO_SOURCE_ID = "demo-tsla-q4-2025"
SPACEX_DEMO_SOURCE_ID = "demo-spacex-s1"
NVDA_EARNINGS_CALL_DEMO_SOURCE_ID = "demo-financial-nvda-q1-fy27-earnings-call"


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
        if (
            normalized.startswith("images/")
            or normalized.startswith("tables/")
            or normalized.startswith("page_citation_assets/")
        ):
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


def _load_source_catalog_module() -> ModuleType:
    configure_import_environment()
    ensure_import_paths()
    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            sys.modules.pop(module_name, None)
    return importlib.import_module("app.services.demo.source_catalog")


def test_should_keep_demo_catalog_metadata_light_for_sources_without_examples(
    monkeypatch: MonkeyPatch,
) -> None:
    source_catalog_module = _load_source_catalog_module()
    loaded_demo_source_ids: list[str] = []
    original_load_source_chunks = source_catalog_module._load_source_chunks

    def load_source_chunks(
        source: Any,
    ) -> tuple[dict[str, Any], ...]:
        loaded_demo_source_ids.append(source.demo_source_id)
        return original_load_source_chunks(source)

    monkeypatch.setattr(
        source_catalog_module,
        "_load_source_chunks",
        load_source_chunks,
    )

    catalog = source_catalog_module.DemoSourceCatalog().get_catalog()

    assert catalog["sources"]
    assert DEMO_SOURCE_ID in loaded_demo_source_ids
    assert SPACEX_DEMO_SOURCE_ID in loaded_demo_source_ids
    assert NVDA_EARNINGS_CALL_DEMO_SOURCE_ID not in loaded_demo_source_ids


def test_should_preserve_filename_rooted_sections_when_publishing_demo_chunks() -> None:
    source_catalog_module = _load_source_catalog_module()
    catalog = source_catalog_module.DemoSourceCatalog()
    source = catalog.require_source(NVDA_EARNINGS_CALL_DEMO_SOURCE_ID)
    publication_chunks = catalog.publication_chunks(source)
    publication_paths = {
        str(chunk.get("path") or "")
        for chunk in publication_chunks
        if chunk.get("type") == "text"
    }

    assert source.title in publication_paths
    assert f"{source.title}/NVIDIA Corp. (NVDA)" in publication_paths
    assert f"{source.title}/MANAGEMENT DISCUSSION SECTION" in publication_paths


@pytest.mark.asyncio
async def test_should_return_demo_catalog_with_resolvable_canonical_citations(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
) -> None:
    async with api_client_factory() as api_client:
        catalog_response = await api_client.get("/api/v1/demo/catalog")

    assert catalog_response.status_code == 200
    catalog = cast(dict[str, Any], catalog_response.json())
    sources = cast(list[dict[str, Any]], catalog["sources"])
    sources_by_id = {str(source["demo_source_id"]): source for source in sources}
    source = sources_by_id[DEMO_SOURCE_ID]
    spacex_source = sources_by_id[SPACEX_DEMO_SOURCE_ID]
    official_library = cast(dict[str, Any], catalog["official_library"])
    library_sources = cast(list[dict[str, Any]], official_library["sources"])
    library_sources_by_id = {
        str(source["library_source_id"]): source for source in library_sources
    }
    examples = cast(list[dict[str, Any]], source["examples"])
    citations = cast(list[dict[str, Any]], examples[0]["citations"])
    citation = citations[0]
    spacex_examples = cast(list[dict[str, Any]], spacex_source["examples"])
    spacex_citations = cast(list[dict[str, Any]], spacex_examples[0]["citations"])

    assert str(sources[0]["demo_source_id"]) == DEMO_SOURCE_ID
    assert SPACEX_DEMO_SOURCE_ID in sources_by_id
    assert source["demo_source_id"] == DEMO_SOURCE_ID
    assert source["canonical_document_id"] == "demo-doc-tsla-q4-2025"
    assert source["chunk_count"] == 71
    assert source["original_file"]["can_download"] is False
    assert citation["canonical_document_id"] == "demo-doc-tsla-q4-2025"
    assert citation["canonical_chunk_id"].startswith(f"{DEMO_SOURCE_ID}:")
    assert spacex_source["official_library"]["library_source_id"] == (
        "financial-spacex-s1"
    )
    assert spacex_source["canonical_document_id"] == "demo-doc-spacex-s1"
    assert spacex_source["chunk_count"] == 922
    assert spacex_citations[0]["canonical_chunk_id"].startswith(
        f"{SPACEX_DEMO_SOURCE_ID}:"
    )
    assert [
        category["category_id"]
        for category in cast(list[dict[str, Any]], official_library["categories"])
    ] == ["financial-reports", "research-papers", "stem-books"]
    assert library_sources_by_id["financial-spacex-s1"]["status"] == "ready"
    assert library_sources_by_id["financial-spacex-s1"]["demo_source_id"] == (
        SPACEX_DEMO_SOURCE_ID
    )
    assert library_sources_by_id["financial-spacex-s1"]["chunk_count"] == 922
    assert library_sources_by_id["stem-statistical-learning"]["status"] == "ready"
    assert library_sources_by_id["stem-statistical-learning"]["demo_source_id"] == (
        "demo-stem-statistical-learning"
    )
    assert library_sources_by_id["stem-statistical-learning"]["chunk_count"] == 71
    research_demo_sources = {
        "research-attention-is-all-you-need": (
            "demo-research-attention-is-all-you-need",
            16,
        ),
        "research-rag-survey": ("demo-research-rag-survey", 37),
        "research-rag-realized": ("demo-research-rag-realized", 41),
        "research-toolformer": ("demo-research-toolformer", 27),
        "research-ai-agents-overview": ("demo-research-ai-agents-overview", 33),
    }
    for library_source_id, (
        expected_demo_source_id,
        expected_chunk_count,
    ) in research_demo_sources.items():
        assert library_sources_by_id[library_source_id]["status"] == "ready"
        assert (
            library_sources_by_id[library_source_id]["demo_source_id"]
            == expected_demo_source_id
        )
        assert (
            library_sources_by_id[library_source_id]["chunk_count"]
            == expected_chunk_count
        )
    assert (
        library_sources_by_id["financial-nvda-q1-fy27-earnings-call"]["demo_source_id"]
        == "demo-financial-nvda-q1-fy27-earnings-call"
    )
    assert library_sources_by_id["financial-micron-report-530bd7ed"]["status"] == (
        "ready"
    )
    assert (
        library_sources_by_id["financial-micron-report-530bd7ed"]["demo_source_id"]
        == "demo-financial-micron-report-530bd7ed"
    )
    assert (
        library_sources_by_id["financial-micron-report-530bd7ed"]["chunk_count"] == 82
    )
    assert (
        library_sources_by_id["financial-micron-report-9c0becf5"]["demo_source_id"]
        == "demo-financial-micron-report-9c0becf5"
    )
    assert (
        library_sources_by_id["financial-micron-report-9c0becf5"]["chunk_count"] == 87
    )
    assert library_sources_by_id["stem-transformers-tutorial"]["demo_source_id"] == (
        "demo-stem-transformers-tutorial"
    )
    assert library_sources_by_id["stem-transformers-tutorial"]["chunk_count"] == 519
    assert library_sources_by_id["stem-information-theory"]["status"] == "planned"

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

    async with developer_api_client_factory() as api_client:
        import app.services.demo.source_materializer as source_materializer_module

        monkeypatch.setattr(
            source_materializer_module,
            "get_result_storage",
            lambda: fake_result_storage,
        )
        empty_cached_response = await api_client.post(
            "/api/v1/retrieval/query",
            json={
                "namespace": "contract-demo",
                "query": "xAI investment",
                "top_k": 5,
                "use_agentic": False,
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
                "use_agentic": False,
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
    assert len(chunk_rows) == 71
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
    assert any(
        file_path.startswith("page_citation_assets/") for file_path in uploaded_files
    )


@pytest.mark.asyncio
async def test_should_materialize_each_normalized_demo_source_once_per_request(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    fake_result_storage = FakeResultStorage()

    async with developer_api_client_factory() as api_client:
        import app.services.demo.source_materializer as source_materializer_module

        monkeypatch.setattr(
            source_materializer_module,
            "get_result_storage",
            lambda: fake_result_storage,
        )
        response = await api_client.post(
            "/api/v1/demo/materializations",
            json={
                "namespace": "contract-demo-deduplicate",
                "demo_source_ids": [
                    DEMO_SOURCE_ID,
                    f" {DEMO_SOURCE_ID} ",
                    DEMO_SOURCE_ID,
                    "",
                ],
            },
        )

    assert response.status_code == 200

    body = cast(dict[str, Any], response.json())
    sources = cast(list[dict[str, Any]], body["sources"])
    assert [source["demo_source_id"] for source in sources] == [DEMO_SOURCE_ID]

    materialization_rows = await ContractDatabase.fetch_all(
        """
        SELECT demo_source_id, document_id
        FROM demo_materializations
        WHERE user_id = 'local-dev-user'
          AND namespace = 'contract-demo-deduplicate'
        """,
    )
    job_rows = await ContractDatabase.fetch_all(
        """
        SELECT job_id
        FROM jobs
        WHERE user_id = 'local-dev-user'
          AND job_metadata ->> 'namespace' = 'contract-demo-deduplicate'
          AND job_metadata ->> 'demo_source_id' = :demo_source_id
        """,
        {"demo_source_id": DEMO_SOURCE_ID},
    )

    assert len(materialization_rows) == 1
    assert len(job_rows) == 1
    assert len(fake_result_storage.raw_files_by_job_id) == 1


@pytest.mark.asyncio
async def test_should_serialize_concurrent_first_demo_materialization(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    fake_result_storage = FakeResultStorage()

    async with developer_api_client_factory() as api_client:
        import app.services.demo.source_materializer as source_materializer_module

        monkeypatch.setattr(
            source_materializer_module,
            "get_result_storage",
            lambda: fake_result_storage,
        )
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

    async with developer_api_client_factory() as api_client:
        import app.services.demo.source_materializer as source_materializer_module

        monkeypatch.setattr(
            source_materializer_module,
            "get_result_storage",
            lambda: fake_result_storage,
        )
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
