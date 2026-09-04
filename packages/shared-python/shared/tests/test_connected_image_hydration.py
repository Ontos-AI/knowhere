"""Regression coverage for connected image/table hydration (#206).

Issue #206: ``assemble_retrieval_results`` marked every ``connect_to`` target as
embedded, then inlined only ``table`` targets. Image targets were dropped from
both the parent content and the standalone result list.

PR #221 (commit ``cb807d18``) dispatches image targets through
``_connected_media_parts`` → ``_image_display_content``. These tests lock that
behavior with synthetic rows only — no documents, LLM, or storage.
"""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from shared.services.retrieval.execution.response_projection import (
    project_public_retrieval_response,
)
from shared.services.retrieval.hydration.result_assembly import (
    assemble_retrieval_results,
)

IMAGE_ASSET_URL = "https://assets.example.com/job-synth/images/flow.png"
TABLE_ASSET_URL = "https://assets.example.com/job-synth/tables/metrics.html"
IMAGE_FILE_PATH = "images/flow.png"


def _connect(*target_ids: str) -> list[dict[str, str]]:
    return [
        {"target": target_id, "relation": "embeds", "ref": f"[{target_id}]"}
        for target_id in target_ids
    ]


def _text_row(
    *,
    chunk_id: str = "text-parent",
    content: str = "The process is illustrated below.",
    connect_to: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "chunk_id": chunk_id,
        "chunk_type": "text",
        "content": content,
        "score": 0.91,
        "document_id": "doc-synth",
        "source_file_name": "synth-manual.pdf",
        "section_path": "Overview / Pipeline",
        "file_path": None,
        "chunk_metadata": {"connect_to": _connect(*(connect_to or []))},
    }
    row.update(extra)
    return row


def _image_row(
    *,
    chunk_id: str = "image-1",
    content: str = "Flowchart of the ingestion pipeline.",
    asset_url: str | None = IMAGE_ASSET_URL,
    file_path: str = IMAGE_FILE_PATH,
    sort_order: int = 20,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "chunk_id": chunk_id,
        "chunk_type": "image",
        "content": content,
        "score": 0.4,
        "document_id": "doc-synth",
        "source_file_name": "synth-manual.pdf",
        "section_path": "Overview / Pipeline",
        "asset_url": asset_url,
        "file_path": file_path,
        "sort_order": sort_order,
        "chunk_metadata": {"page_nums": [12]},
    }
    row.update(extra)
    return row


def _table_row(
    *,
    chunk_id: str = "table-1",
    sort_order: int = 10,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "chunk_id": chunk_id,
        "chunk_type": "table",
        "content": "<table><tr><td>SHOULD NOT LEAK</td></tr></table>",
        "score": 0.5,
        "document_id": "doc-synth",
        "source_file_name": "synth-manual.pdf",
        "section_path": "Overview / Pipeline",
        "asset_url": TABLE_ASSET_URL,
        "file_path": "tables/metrics.html",
        "sort_order": sort_order,
        "chunk_metadata": {
            "summary": "Pipeline stage latency",
            "keywords": ["parse", "embed"],
            "caption": "Stage timings",
            "page_nums": [12],
        },
    }
    row.update(extra)
    return row


def _page_row(*, chunk_id: str = "page-1") -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "chunk_type": "page",
        "content": "RAW OCR SHOULD NOT LEAK",
        "score": 0.7,
        "document_id": "doc-synth",
        "source_file_name": "synth-manual.pdf",
        "section_path": "Overview",
        "chunk_metadata": {
            "summary": "Pipeline overview page",
            "page_nums": [12, 13],
        },
    }


async def _assemble(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return await assemble_retrieval_results(
        rows=rows,
        exclude_document_ids=[],
        exclude_sections=[],
    )


@pytest.mark.asyncio
async def test_text_chunk_inlines_connected_image_and_does_not_drop_it() -> None:
    assembled = await _assemble(
        [
            _text_row(connect_to=["image-1"]),
            _image_row(),
        ]
    )

    assert len(assembled) == 1
    parent = assembled[0]
    assert parent["chunk_id"] == "text-parent"
    assert "The process is illustrated below." in parent["content"]
    assert f"[Image: {IMAGE_ASSET_URL}]" in parent["content"]
    assert "Flowchart of the ingestion pipeline." in parent["content"]
    assert parent["content"].index("[Image:") > parent["content"].index(
        "The process is illustrated below."
    )


@pytest.mark.asyncio
async def test_text_chunk_composes_connected_table_summary() -> None:
    assembled = await _assemble(
        [
            _text_row(
                content="Latency is summarized in the table.",
                connect_to=["table-1"],
            ),
            _table_row(),
        ]
    )

    assert len(assembled) == 1
    content = assembled[0]["content"]
    assert "Latency is summarized in the table." in content
    assert f"[Table: {TABLE_ASSET_URL}]" in content
    assert "Pipeline stage latency" in content
    assert "parse;embed" in content
    assert "Stage timings" in content
    assert "SHOULD NOT LEAK" not in content
    assert "<table" not in content


@pytest.mark.asyncio
async def test_connected_targets_are_not_emitted_as_standalone_results() -> None:
    assembled = await _assemble(
        [
            _text_row(connect_to=["image-1", "table-1"]),
            _image_row(),
            _table_row(),
            _image_row(
                chunk_id="image-standalone",
                content="Unrelated photo of the plant floor.",
                asset_url="https://assets.example.com/job-synth/images/plant.png",
                file_path="images/plant.png",
                sort_order=99,
            ),
        ]
    )

    chunk_ids = [row["chunk_id"] for row in assembled]
    assert chunk_ids == ["text-parent", "image-standalone"]
    parent_content = assembled[0]["content"]
    assert f"[Image: {IMAGE_ASSET_URL}]" in parent_content
    assert f"[Table: {TABLE_ASSET_URL}]" in parent_content
    assert "Unrelated photo of the plant floor." not in parent_content
    assert assembled[1]["content"] == "Unrelated photo of the plant floor."


@pytest.mark.asyncio
async def test_connected_image_falls_back_to_file_path_without_asset_url() -> None:
    assembled = await _assemble(
        [
            _text_row(connect_to=["image-1"]),
            _image_row(asset_url=None, file_path=IMAGE_FILE_PATH),
        ]
    )

    assert f"[Image: {IMAGE_FILE_PATH}]" in assembled[0]["content"]
    assert "Flowchart of the ingestion pipeline." in assembled[0]["content"]


@pytest.mark.asyncio
async def test_source_projection_keeps_page_nums_and_related_asset_fields() -> None:
    assembled = await _assemble(
        [
            _text_row(
                connect_to=["image-1"],
                file_path="sections/overview.txt",
                source_chunk_path="synth-manual.pdf/Overview/Pipeline",
            ),
            _image_row(),
            _page_row(),
        ]
    )

    projected = await project_public_retrieval_response(
        {
            "namespace": "knowledge",
            "query": "pipeline flowchart",
            "router_used": "classic_topk",
            "results": assembled,
        }
    )
    results = projected["results"]
    by_chunk_id = {row["chunk_id"]: row for row in results}

    assert set(by_chunk_id) == {"text-parent", "page-1"}

    text_result = by_chunk_id["text-parent"]
    assert text_result["source"] == {
        "document_id": "doc-synth",
        "source_file_name": "synth-manual.pdf",
        "section_path": "Overview / Pipeline",
    }
    assert text_result["file_path"] == "sections/overview.txt"
    assert text_result["source_chunk_path"] == "synth-manual.pdf/Overview/Pipeline"
    assert f"[Image: {IMAGE_ASSET_URL}]" in text_result["content"]
    assert "asset_url" not in text_result

    page_result = by_chunk_id["page-1"]
    assert page_result["source"] == {
        "document_id": "doc-synth",
        "source_file_name": "synth-manual.pdf",
        "section_path": "Overview",
        "page_nums": [12, 13],
    }
    assert page_result["content"] == "Pipeline overview page"
    assert "RAW OCR SHOULD NOT LEAK" not in page_result["content"]
