from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.retrieval.hydration.assets import (  # noqa: E402
    build_retrieval_asset_url_map,
    enrich_rows_with_retrieval_asset_url,
)
from shared.services.retrieval.hydration.result_assembly import (  # noqa: E402
    assemble_retrieval_results,
)
from shared.services.retrieval.search.lexical_text import (  # noqa: E402
    build_content_lexical_text,
    build_content_search_text,
    build_term_search_text,
)
from shared.services.retrieval.settings import normalize_chunk_types  # noqa: E402
from shared.services.retrieval.execution.reference_resolver import (  # noqa: E402
    resolve_workflow_references,
)
from shared.services.storage.result_storage import JobResultStorage  # noqa: E402
from shared.services.storage.page_pdf_crop import crop_source_pdf_pages  # noqa: E402


def test_data_type_one_allows_page_and_page_content_enters_search_text() -> None:
    page_chunk = {
        "type": "page",
        "content": "安全风险分级管控 raw pymupdf content",
        "metadata": {"summary": "node summary is not the primary content"},
    }

    assert normalize_chunk_types(None) is None
    content_search_text = build_content_search_text(page_chunk) or ""
    assert "安全" in content_search_text
    assert "风险" in content_search_text
    assert "安全风险" in (build_term_search_text(page_chunk, path_text="安全类") or "")


def test_data_type_seven_is_page_only() -> None:
    assert normalize_chunk_types(["page"]) == {"page"}


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
    assert "tables/table-1.html" not in content_lexical_text
    assert "tables/table-1.html" not in term_search_text


@pytest.mark.asyncio
async def test_page_result_assembly_uses_summary_not_raw_content() -> None:
    rows = [
        {
            "chunk_id": "page-node-1",
            "chunk_type": "page",
            "content": "RAW OCR SHOULD NOT LEAK",
            "chunk_metadata": {
                "summary": "制度标准总则摘要",
                "page_nums": [225],
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
async def test_page_asset_url_is_generated_from_page_nums(monkeypatch) -> None:
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.assets.crop_source_pdf_pages",
        lambda *, job_id, pages: (
            f"https://assets.example.com/{job_id}/page_pdfs/{'-'.join(map(str, pages))}.pdf"
        ),
    )

    rows = [
        {
            "chunk_id": "page-node-1",
            "chunk_type": "page",
            "job_id": "job-1",
            "chunk_metadata": {
                "page_nums": [225, 226, 225],
            },
        }
    ]

    enriched = await enrich_rows_with_retrieval_asset_url(
        rows,
        log_context="contract",
    )
    url_map = await build_retrieval_asset_url_map(rows, log_context="contract")

    assert enriched[0]["asset_url"] == (
        "https://assets.example.com/job-1/page_pdfs/225-226.pdf"
    )
    assert "asset_urls" not in enriched[0]
    assert url_map["page-node-1"] == enriched[0]["asset_url"]


@pytest.mark.asyncio
async def test_page_citation_asset_precedes_lazy_page_pdf_fallback(monkeypatch) -> None:
    page_pdf_calls: list[tuple[str, list[int]]] = []

    def fake_crop_source_pdf_pages(*, job_id, pages):
        page_pdf_calls.append((job_id, pages))
        return f"https://assets.example.com/{job_id}/page_pdfs/{'-'.join(map(str, pages))}.pdf"

    class FakeResultStorage:
        def normalize_artifact_ref(self, artifact_ref: str | None) -> str | None:
            if artifact_ref == "page_citation_assets/page-225.png":
                return artifact_ref
            return None

        def generate_artifact_url(
            self,
            *,
            job_id: str,
            artifact_ref: str,
            expires_in: int = 3600,
        ) -> str:
            del expires_in
            return f"https://assets.example.com/{job_id}/{artifact_ref}"

    monkeypatch.setattr(
        "shared.services.retrieval.hydration.assets.crop_source_pdf_pages",
        fake_crop_source_pdf_pages,
    )
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
                "page_nums": [225, 226],
                "page_assets": [
                    {
                        "page_num": 225,
                        "artifact_ref": "page_citation_assets/page-225.png",
                        "content_type": "image/png",
                        "width": 1200,
                        "height": 1800,
                        "source": "knowhere-rendered-page-citation-source",
                    }
                ],
            },
        }
    ]

    enriched = await enrich_rows_with_retrieval_asset_url(
        rows,
        log_context="contract",
    )
    url_map = await build_retrieval_asset_url_map(rows, log_context="contract")

    expected_url = "https://assets.example.com/job-1/page_citation_assets/page-225.png"
    assert enriched[0]["asset_url"] == expected_url
    assert enriched[0]["metadata"]["page_assets"][0]["asset_url"] == expected_url
    assert url_map["page-node-1"] == expected_url
    assert page_pdf_calls == []


def test_result_storage_allows_page_citation_and_page_pdf_artifact_refs_not_debug_page_pngs() -> None:
    storage = JobResultStorage(results_bucket="test-results")

    assert storage.normalize_artifact_ref("pages/page-225.png") is None
    assert (
        storage.normalize_artifact_ref("page_citation_assets/page-225.png")
        == "page_citation_assets/page-225.png"
    )
    assert (
        storage.normalize_artifact_ref("page_pdfs/page-225.pdf")
        == "page_pdfs/page-225.pdf"
    )


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
    (result_dir / "page_citation_assets").mkdir()
    (result_dir / "tables").mkdir()
    (result_dir / "source.pdf").write_bytes(b"source")
    (result_dir / "pages" / "page-225.png").write_bytes(b"anchored")
    (result_dir / "pages" / "page-999.png").write_bytes(b"unanchored")
    (result_dir / "page_citation_assets" / "page-225.png").write_bytes(b"citation")
    (result_dir / "page_citation_assets" / "page-999.png").write_bytes(b"unreferenced")
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
        artifact_refs={
            "source.pdf",
            "pages/page-225.png",
            "page_citation_assets/page-225.png",
            "tables/table-1.html",
        },
    )

    assert set(bundle.raw_files) == {
        "source.pdf",
        "page_citation_assets/page-225.png",
        "tables/table-1.html",
    }
    assert "results/job-1/source.pdf" in adapter.uploaded_keys
    assert "results/job-1/tables/table-1.html" in adapter.uploaded_keys
    assert "results/job-1/page_citation_assets/page-225.png" in adapter.uploaded_keys
    assert "results/job-1/pages/page-225.png" not in adapter.uploaded_keys
    assert "results/job-1/pages/page-999.png" not in adapter.uploaded_keys
    assert "results/job-1/page_citation_assets/page-999.png" not in adapter.uploaded_keys
    assert "results/job-1/debug.csv" not in adapter.uploaded_keys


def test_crop_source_pdf_pages_uploads_and_reuses_page_pdf_cache(tmp_path) -> None:
    from pypdf import PdfReader, PdfWriter

    source_pdf = tmp_path / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    with source_pdf.open("wb") as file_obj:
        writer.write(file_obj)

    class FakeResultStorage:
        def __init__(self) -> None:
            self.files: dict[str, Path] = {"source.pdf": source_pdf}
            self.upload_count = 0

        def verify_raw_exists(self, *, job_id: str, relative_path: str) -> bool:
            del job_id
            return relative_path in self.files

        def download_raw_to_temp(
            self,
            *,
            job_id: str,
            relative_path: str,
            suffix: str,
            temp_dir: str,
        ) -> str:
            del job_id, suffix
            target = Path(temp_dir) / Path(relative_path).name
            shutil.copyfile(self.files[relative_path], target)
            return str(target)

        def upload_raw_file(
            self,
            *,
            job_id: str,
            relative_path: str,
            local_file_path: str,
        ) -> None:
            del job_id
            target = tmp_path / relative_path.replace("/", "_")
            shutil.copyfile(local_file_path, target)
            self.files[relative_path] = target
            self.upload_count += 1

        def generate_artifact_url(
            self,
            *,
            job_id: str,
            artifact_ref: str,
            expires_in: int = 3600,
        ) -> str:
            del expires_in
            return f"https://assets.example.com/{job_id}/{artifact_ref}"

    storage = FakeResultStorage()

    url = crop_source_pdf_pages(
        job_id="job-1",
        pages=[2, 1, 2],
        storage=storage,  # type: ignore[arg-type]
        temp_dir=str(tmp_path),
    )
    assert url and "/page_pdfs/" in url
    page_pdf_refs = [ref for ref in storage.files if ref.startswith("page_pdfs/")]
    assert len(page_pdf_refs) == 1
    assert len(PdfReader(str(storage.files[page_pdf_refs[0]])).pages) == 2

    cached_url = crop_source_pdf_pages(
        job_id="job-1",
        pages=[1, 2],
        storage=storage,  # type: ignore[arg-type]
        temp_dir=str(tmp_path),
    )
    assert cached_url == url
    assert storage.upload_count == 1


def test_crop_source_pdf_pages_returns_none_when_source_pdf_is_missing(tmp_path) -> None:
    class FakeResultStorage:
        def __init__(self) -> None:
            self.upload_count = 0

        def verify_raw_exists(self, *, job_id: str, relative_path: str) -> bool:
            del job_id, relative_path
            return False

        def download_raw_to_temp(self, **_kwargs) -> str:
            raise AssertionError("source download should not be attempted")

        def upload_raw_file(self, **_kwargs) -> None:
            self.upload_count += 1

        def generate_artifact_url(self, **_kwargs) -> str:
            raise AssertionError("URL generation should not be attempted")

    storage = FakeResultStorage()

    assert (
        crop_source_pdf_pages(
            job_id="job-missing-source",
            pages=[1],
            storage=storage,  # type: ignore[arg-type]
            temp_dir=str(tmp_path),
        )
        is None
    )
    assert storage.upload_count == 0


@pytest.mark.asyncio
async def test_referenced_chunks_get_page_asset_url_from_hydrated_rows(
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
                "chunk_metadata": {"page_nums": [225]},
                "job_id": "job-1",
            }
        ]

    async def fake_enrich_referenced_chunks_with_asset_url(rows):
        enriched = []
        for row in rows:
            enriched.append(
                {
                    **row,
                    "asset_url": (
                        "https://assets.example.com/job-1/page_pdfs/page-225.pdf"
                    ),
                }
            )
        return enriched

    monkeypatch.setattr(
        "shared.services.retrieval.execution.reference_resolver.hydrate_referenced_chunk_rows",
        fake_hydrate_referenced_chunk_rows,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.execution.reference_resolver.enrich_referenced_chunks_with_asset_url",
        fake_enrich_referenced_chunks_with_asset_url,
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

    assert resolved.refs[0]["asset_url"] == (
        "https://assets.example.com/job-1/page_pdfs/page-225.pdf"
    )
    assert "asset_urls" not in resolved.refs[0]
