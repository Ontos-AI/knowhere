from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.retrieval.hydration.assets import (  # noqa: E402
    build_retrieval_asset_url_map,
    enrich_rows_with_retrieval_asset_urls,
)
from shared.services.retrieval.hydration.result_assembly import (  # noqa: E402
    assemble_retrieval_results,
)
from shared.services.retrieval.search.lexical_text import (  # noqa: E402
    build_content_lexical_text,
    build_content_search_text,
    build_term_search_text,
)
from shared.services.retrieval.settings import resolve_allowed_chunk_types  # noqa: E402
from shared.services.retrieval.execution.reference_resolver import (  # noqa: E402
    resolve_workflow_references,
)
from shared.services.storage.result_storage import JobResultStorage  # noqa: E402


def test_data_type_one_allows_page_and_page_content_enters_search_text() -> None:
    page_chunk = {
        "type": "page",
        "content": "安全风险分级管控 raw pymupdf content",
        "metadata": {"summary": "node summary is not the primary content"},
    }

    assert resolve_allowed_chunk_types(1) is None
    content_search_text = build_content_search_text(page_chunk) or ""
    assert "安全" in content_search_text
    assert "风险" in content_search_text
    assert "安全风险" in (build_term_search_text(page_chunk, path_text="安全类") or "")


def test_data_type_seven_is_page_only() -> None:
    assert resolve_allowed_chunk_types(7) == {"page"}


def test_table_search_text_uses_summary_keywords_and_caption_not_content() -> None:
    table_chunk = {
        "type": "table",
        "content": "tables/table-1.html",
        "metadata": {
            "summary": "企业入驻信息登记模板",
            "keywords": ["企业名称", "统一社会信用代码"],
            "caption": "批量录入表",
        },
    }

    content_search_text = build_content_search_text(table_chunk) or ""
    content_lexical_text = build_content_lexical_text(table_chunk) or ""
    term_search_text = build_term_search_text(
        table_chunk,
        path_text="企业批量录入",
    ) or ""

    assert "企业" in content_search_text
    assert "信用" in content_search_text
    assert "批量录入表" in content_lexical_text
    assert "企业批量录入" in term_search_text
    assert "tables/table-1.html" not in content_search_text


@pytest.mark.asyncio
async def test_page_result_assembly_uses_summary_not_raw_content() -> None:
    rows = [
        {
            "chunk_id": "page-node-1",
            "chunk_type": "page",
            "content": "RAW OCR SHOULD NOT LEAK",
            "chunk_metadata": {
                "summary": "制度标准总则摘要",
                "page_image_uris": ["pages/page-225.png"],
            },
        }
    ]

    assembled = await assemble_retrieval_results(
        rows=rows,
        exclude_document_ids=[],
        exclude_sections=[],
    )

    assert assembled[0]["content"] == "制度标准总则摘要"


@pytest.mark.asyncio
async def test_table_result_assembly_uses_summary_not_html() -> None:
    rows = [
        {
            "chunk_id": "text-1",
            "chunk_type": "text",
            "content": "见表 [tables/table-1.html]",
            "chunk_metadata": {
                "connect_to": [
                    {
                        "target": "table-1",
                        "relation": "embeds",
                        "ref": "[tables/table-1.html]",
                    }
                ]
            },
        },
        {
            "chunk_id": "table-1",
            "chunk_type": "table",
            "content": "<table><tr><td>SHOULD NOT LEAK</td></tr></table>",
            "file_path": "tables/table-1.html",
            "asset_url": "https://assets.example.com/job-1/tables/table-1.html",
            "chunk_metadata": {
                "summary": "企业入驻信息登记模板",
                "keywords": ["企业名称", "统一社会信用代码"],
            },
        },
    ]

    assembled = await assemble_retrieval_results(
        rows=rows,
        exclude_document_ids=[],
        exclude_sections=[],
    )

    assert len(assembled) == 1
    content = assembled[0]["content"]
    assert "[Table: https://assets.example.com/job-1/tables/table-1.html]" in content
    assert "企业入驻信息登记模板" in content
    assert "企业名称;统一社会信用代码" in content
    assert "SHOULD NOT LEAK" not in content
    assert "<table" not in content


@pytest.mark.asyncio
async def test_page_asset_urls_are_generated_from_page_image_uris(monkeypatch) -> None:
    class FakeResultStorage:
        def normalize_artifact_ref(self, artifact_ref: str | None) -> str | None:
            if not artifact_ref:
                return None
            normalized = artifact_ref.strip().replace("\\", "/").lstrip("/")
            root = normalized.split("/", 1)[0]
            if root not in {"images", "tables", "pages"}:
                return None
            return normalized

        def generate_artifact_url(
            self,
            *,
            job_id: str,
            artifact_ref: str,
            expires_in: int = 3600,
        ) -> str | None:
            del expires_in
            return f"https://assets.example.com/{job_id}/{artifact_ref}"

    monkeypatch.setattr(
        "shared.services.retrieval.hydration.assets.get_result_storage",
        lambda: FakeResultStorage(),
    )

    rows = [
        {
            "chunk_id": "page-node-1",
            "chunk_type": "page",
            "job_id": "job-1",
            "chunk_metadata": {
                "page_image_uris": [
                    "pages/page-225.png",
                    "pages/page-226.png",
                    "../images/not-allowed.png",
                    "pages/page-225.png",
                ]
            },
        }
    ]

    enriched = await enrich_rows_with_retrieval_asset_urls(
        rows,
        log_context="contract",
    )
    url_map = await build_retrieval_asset_url_map(rows, log_context="contract")

    assert enriched[0]["asset_urls"] == [
        "https://assets.example.com/job-1/pages/page-225.png",
        "https://assets.example.com/job-1/pages/page-226.png",
    ]
    assert url_map["page-node-1"] == enriched[0]["asset_urls"]


def test_result_storage_allows_pages_artifact_refs() -> None:
    storage = JobResultStorage(results_bucket="test-results")

    assert storage.normalize_artifact_ref("pages/page-225.png") == "pages/page-225.png"
    assert storage.normalize_artifact_ref("../pages/page-226.png") == "pages/page-226.png"


def test_result_storage_upload_filters_to_referenced_artifacts(tmp_path) -> None:
    class FakeStorageAdapter:
        def __init__(self) -> None:
            self.uploaded_keys: list[str] = []

        def upload_file(self, local_path: str, key: str, bucket: str | None = None):
            del local_path, bucket
            self.uploaded_keys.append(key)
            return {"key": key}

        def generate_presigned_url(self, *args, **kwargs) -> str:
            del args, kwargs
            return "https://assets.example.test/file"

    result_dir = tmp_path / "result"
    (result_dir / "pages").mkdir(parents=True)
    (result_dir / "tables").mkdir()
    (result_dir / "pages" / "page-225.png").write_bytes(b"anchored")
    (result_dir / "pages" / "page-999.png").write_bytes(b"unanchored")
    (result_dir / "tables" / "table-1.html").write_text("<table></table>")
    (result_dir / "debug.csv").write_text("debug")
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"zip")

    adapter = FakeStorageAdapter()
    storage = JobResultStorage(
        results_bucket="test-results",
        storage_adapter=adapter,  # type: ignore[arg-type]
    )

    bundle = storage.upload(
        job_id="job-1",
        result_dir=str(result_dir),
        zip_file_path=str(zip_path),
        artifact_refs={"pages/page-225.png", "tables/table-1.html"},
    )

    assert set(bundle.raw_files) == {"pages/page-225.png", "tables/table-1.html"}
    assert "results/job-1/pages/page-225.png" in adapter.uploaded_keys
    assert "results/job-1/tables/table-1.html" in adapter.uploaded_keys
    assert "results/job-1/pages/page-999.png" not in adapter.uploaded_keys
    assert "results/job-1/debug.csv" not in adapter.uploaded_keys


@pytest.mark.asyncio
async def test_referenced_chunks_get_page_asset_urls_from_hydrated_rows(
    monkeypatch,
) -> None:
    async def fake_hydrate_referenced_chunk_rows(**_kwargs):
        return [
            {
                "document_id": "doc-1",
                "chunk_id": "page-node-1",
                "chunk_type": "page",
                "section_path": "安全类 / 1 总则",
                "file_path": None,
                "chunk_metadata": {"page_image_uris": ["pages/page-225.png"]},
                "job_id": "job-1",
            }
        ]

    async def fake_enrich_referenced_chunks_with_asset_urls(rows):
        enriched = []
        for row in rows:
            enriched.append(
                {
                    **row,
                    "asset_urls": [
                        "https://assets.example.com/job-1/pages/page-225.png"
                    ],
                }
            )
        return enriched

    monkeypatch.setattr(
        "shared.services.retrieval.execution.reference_resolver.hydrate_referenced_chunk_rows",
        fake_hydrate_referenced_chunk_rows,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.execution.reference_resolver.enrich_referenced_chunks_with_asset_urls",
        fake_enrich_referenced_chunks_with_asset_urls,
    )

    resolved = await resolve_workflow_references(
        db=None,  # fake hydrate ignores db
        user_id="user-1",
        namespace="default",
        refs=[
            {
                "document_id": "doc-1",
                "chunk_id": "page-node-1",
                "chunk_type": "page",
                "section_path": "安全类 / 1 总则",
            }
        ],
    )

    assert resolved.refs[0]["asset_urls"] == [
        "https://assets.example.com/job-1/pages/page-225.png"
    ]
