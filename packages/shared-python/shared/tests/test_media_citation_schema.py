from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from shared.services.retrieval.execution.reference_resolver import (
    _merge_reference_projection,
)
from shared.services.retrieval.execution.response_projection import (
    project_public_retrieval_response,
)
from shared.services.retrieval.hydration.result_assembly import (
    assemble_retrieval_results,
)
from shared.services.storage.zip_chunk_schema import ZipChunkSchemaBuilder

_MEDIA_METADATA = {
    "time_ranges": [{"start_ms": 1000, "end_ms": 2500}],
    "speakers": ["S1"],
    "media": {"kind": "audio", "duration_ms": 12500},
    "source_kind": "asr",
}


def test_zip_text_branch_keeps_media_citation_fields() -> None:
    formatted = ZipChunkSchemaBuilder().format_chunks(
        [
            {
                "chunk_id": "media-1",
                "type": "text",
                "content": "S1: hello",
                "path": "talk.mp3",
                "metadata": {
                    "length": 9,
                    "summary": "",
                    "page_nums": [],
                    "keywords": [],
                    **_MEDIA_METADATA,
                },
            },
            {
                "chunk_id": "plain-1",
                "type": "text",
                "content": "ordinary paragraph",
                "path": "notes.md/Intro",
                "metadata": {
                    "length": 19,
                    "summary": "",
                    "page_nums": [],
                    "keywords": [],
                },
            },
        ],
        image_files_map={},
        table_files_map={},
    )
    by_id = {chunk["chunk_id"]: chunk["metadata"] for chunk in formatted}
    for key, value in _MEDIA_METADATA.items():
        assert by_id["media-1"][key] == value
        assert key not in by_id["plain-1"]


@pytest.mark.asyncio
async def test_public_citation_copies_time_ranges_like_page_nums() -> None:
    assembled = await assemble_retrieval_results(
        rows=[
            {
                "document_id": "doc_media",
                "chunk_id": "media-1",
                "chunk_type": "text",
                "content": "S1: hello",
                "source_file_name": "talk.mp3",
                "section_path": "talk.mp3",
                "job_result_id": "jr_1",
                "sort_order": 0,
                "chunk_metadata": dict(_MEDIA_METADATA),
            }
        ],
        exclude_document_ids=[],
        exclude_sections=[],
    )
    assert assembled[0]["time_ranges"] == _MEDIA_METADATA["time_ranges"]
    public = await project_public_retrieval_response({"results": assembled})
    source = public["results"][0]["source"]
    assert source["time_ranges"] == _MEDIA_METADATA["time_ranges"]
    assert source["document_id"] == "doc_media"
    assert "speakers" not in source
    assert public["results"][0]["metadata"]["speakers"] == ["S1"]
    assert public["results"][0]["metadata"]["source_kind"] == "asr"


@pytest.mark.asyncio
async def test_ordinary_text_citation_omits_time_ranges() -> None:
    assembled = await assemble_retrieval_results(
        rows=[
            {
                "document_id": "doc_text",
                "chunk_id": "plain-1",
                "chunk_type": "text",
                "content": "ordinary paragraph",
                "source_file_name": "notes.md",
                "section_path": "notes.md / Intro",
                "job_result_id": "jr_2",
                "sort_order": 0,
                "chunk_metadata": {"keywords": []},
            }
        ],
        exclude_document_ids=[],
        exclude_sections=[],
    )
    assert "time_ranges" not in assembled[0]
    public = await project_public_retrieval_response({"results": assembled})
    assert "time_ranges" not in public["results"][0]["source"]


def test_reference_projection_copies_time_ranges() -> None:
    refs = [
        {
            "document_id": "doc_media",
            "chunk_id": "media-1",
            "section_path": "talk.mp3",
            "file_path": "",
        }
    ]
    rows = [
        {
            "document_id": "doc_media",
            "chunk_id": "media-1",
            "section_path": "talk.mp3",
            "file_path": "",
            "job_result_id": "jr_1",
            "chunk_metadata": dict(_MEDIA_METADATA),
        }
    ]
    merged = _merge_reference_projection(refs, rows)
    assert merged[0]["time_ranges"] == _MEDIA_METADATA["time_ranges"]
    assert merged[0]["job_result_id"] == "jr_1"
