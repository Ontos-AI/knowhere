"""Body-only PAGE previews survive parser ownership and result serialization."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")


def test_profile_preserves_classified_body_without_changing_full_text(monkeypatch) -> None:
    from app.services.document_agent.pdf_text import LineDraft, build_page_text_bands
    from app.services.document_parser.profiling import doc_profiler
    from app.services.document_agent.manifest import DocumentProfile

    bands = build_page_text_bands([
        LineDraft("iv", region="header"),
        LineDraft("Table of Contents", region="header"),
        LineDraft("GLOSSARY OF TERMS", region="body"),
        LineDraft("Actual body text", region="body"),
        LineDraft("20", region="footer"),
    ])

    class Coordinator:
        def __init__(self, **kwargs):
            self.blackboard = SimpleNamespace(
                page_count=3, doc_stats={}, global_signals={},
                page_full_text_cache={
                    1: bands, 2: "Legacy unclassified text",
                    3: build_page_text_bands([LineDraft("header", region="header")]),
                }, toc_result=None,
            )

        def run_coarse(self):
            return DocumentProfile(False, ["04000000"], "Report", "generic")

        def run_lightweight_anatomy(self, **kwargs):
            return None

    monkeypatch.setattr(doc_profiler, "ProfileCoordinator", Coordinator)
    profile = doc_profiler._profile_pdf_with_db(
        file_path="unused.pdf", filename="unused.pdf", job_id=None,
        output_dir=None, db=None,
    )
    assert profile.page_full_text_cache[1] == bands.content
    assert profile.page_body_text_cache == {1: "GLOSSARY OF TERMS\nActual body text", 2: "", 3: ""}
    assert "page_body_text_cache" not in profile.to_dict()


@pytest.mark.parametrize("has_body_cache", [True, False])
def test_page_preview_respects_owned_pages_and_same_page_sections(
    tmp_path: Path, monkeypatch, has_body_cache: bool,
) -> None:
    from app.services.page_memory import memory_service, page_renderer, page_tagger, skeleton_extractor
    from app.services.page_memory.page_tagger import PageTagResult
    from app.services.page_memory.skeleton_extractor import SectionSkeleton
    from shared.models.schemas.page_memory_config import PageMemoryConfig
    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
    from shared.services.storage.zip_chunk_schema import ZipChunkSchemaBuilder

    skeletons = [
        SectionSkeleton("doc.pdf/First", 1, 1, 1, "First", "doc.pdf"),
        SectionSkeleton("doc.pdf/SharedOnly", 1, 1, 1, "SharedOnly", "doc.pdf"),
        SectionSkeleton("doc.pdf/Second", 1, 1, 2, "Second", "doc.pdf"),
        SectionSkeleton("doc.pdf/Empty", 1, 3, 3, "Empty", "doc.pdf"),
    ]
    raw = {1: "iv\nTable of Contents\nFirst\nSharedOnly\nSHARED BODY\nSecond\nSECOND BODY\nfooter", 2: "header\nSecond continuation\nfooter", 3: "Only header and footer"}
    profile = SimpleNamespace(page_count=3, anatomy=SimpleNamespace(page_features=[], page_labels=[], toc_result=None), page_full_text_cache=raw)
    if has_body_cache:
        profile.page_body_text_cache = {1: "First\nSharedOnly\nSHARED BODY\nSecond\nSECOND BODY", 2: "Second continuation", 3: ""}

    def render(ctx, pages, **kwargs):
        directory = tmp_path / kwargs["folder_name"]
        directory.mkdir(parents=True, exist_ok=True)
        output = []
        for page in pages:
            path = directory / f"page-{page}.png"
            Image.new("RGB", (2, 3), color="white").save(path)
            output.append({"page": page, "png_path": str(path)})
        return output

    monkeypatch.delenv("IMAGE_MODEL", raising=False)
    monkeypatch.setattr(skeleton_extractor, "extract_section_skeletons", lambda **kwargs: skeletons)
    monkeypatch.setattr(page_renderer, "render_pages", render)
    monkeypatch.setattr(page_tagger, "tag_pages", lambda **kwargs: [PageTagResult(page_index=page.page_index, summary="summary") for page in kwargs["pages"]])
    dataframe = memory_service._build_page_dataframe(
        pdf_path="unused.pdf", filename="doc.pdf", output_dir=str(tmp_path),
        profile=profile, verdict="page", page_memory_config=PageMemoryConfig(fine_min_pages=100, scope_concurrency=1, asset_extraction_enabled=False),
    )
    chunks = dataframe_to_chunks(dataframe)
    zipped = ZipChunkSchemaBuilder().format_chunks(chunks, {}, {})
    for output in [chunks, zipped]:
        by_path = {chunk["path"]: chunk for chunk in output}
        assert by_path["doc.pdf/First"]["content"] == raw[1]
        assert "SAME-AS doc.pdf/First p1" in by_path["doc.pdf/Second"]["content"]
        assert by_path["doc.pdf/First"]["metadata"]["body_text_preview"] == ("First" if has_body_cache else "")
        assert by_path["doc.pdf/SharedOnly"]["metadata"]["body_text_preview"] == ""
        assert by_path["doc.pdf/Second"]["metadata"]["body_text_preview"] == ("Second continuation" if has_body_cache else "")
        assert by_path["doc.pdf/Empty"]["metadata"]["body_text_preview"] == ""


def test_whole_document_preview_is_body_only_and_bounded() -> None:
    from app.services.page_memory.memory_service import _build_whole_doc_dataframe
    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks

    dataframe = _build_whole_doc_dataframe(
        filename="whole.pdf", page_count=2, verdict="whole_doc",
        page_texts={1: "header\n" + "body " * 100, 2: "footer"},
        body_text_by_page={1: "body " * 100, 2: ""},
    )
    chunk = dataframe_to_chunks(dataframe)[0]
    assert chunk["metadata"]["body_text_preview"] == ("body " * 100).strip()[:180]
    assert "header" in chunk["content"] and "footer" in chunk["content"]
