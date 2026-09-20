from __future__ import annotations

import base64

import pytest

from shared.services.retrieval.hydration.evidence_compose import (
    compose_evidence_parts,
    flatten_parts,
)
from shared.services.retrieval.hydration.result_assembly import assemble_retrieval_results


def test_flatten_parts_keeps_html_and_encodes_images() -> None:
    text = flatten_parts(
        [
            {"type": "text", "text": "before"},
            {"type": "image", "media_type": "image/png", "data": "abc"},
            {"type": "text", "text": "after"},
        ]
    )
    assert text == "beforedata:image/png;base64,abcafter"


def test_page_parts_are_summary_then_page_image(monkeypatch) -> None:
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.evidence_compose._try_read_image_artifact",
        lambda row, artifact, media_type: {
            "type": "image",
            "media_type": media_type,
            "data": base64.b64encode(b"page").decode("ascii"),
        },
    )
    parts = compose_evidence_parts(
        {
            "chunk_type": "page",
            "chunk_metadata": {
                "summary": "制度摘要",
                "page_nums": [4],
                "page_assets": [
                    {
                        "page_num": 4,
                        "artifact_ref": "page_citation_assets/page-4.png",
                        "content_type": "image/png",
                    }
                ],
            },
        },
        {},
    )
    assert parts[0] == {"type": "text", "text": "制度摘要"}
    assert parts[1]["type"] == "image"
    assert parts[1]["media_type"] == "image/png"


def test_page_parts_do_not_inline_connected_charts() -> None:
    parts = compose_evidence_parts(
        {
            "chunk_type": "page",
            "content": "RAW",
            "chunk_metadata": {
                "summary": "只要摘要",
                "connect_to": [
                    {
                        "target": "table-1",
                        "relation": "embeds",
                        "ref": "[tables/a.html]",
                    }
                ],
            },
        },
        {
            "table-1": {
                "chunk_type": "table",
                "content": "<table><tr><td>no</td></tr></table>",
            }
        },
    )
    assert parts == [{"type": "text", "text": "只要摘要"}]


def test_standalone_table_uses_html() -> None:
    parts = compose_evidence_parts(
        {
            "chunk_type": "table",
            "content": "<table><tr><td>Q4</td></tr></table>",
            "file_path": "tables/a.html",
        },
        {},
    )
    assert parts[0]["type"] == "text"
    assert "<table><tr><td>Q4</td></tr></table>" in parts[0]["text"]


def test_standalone_image_is_bytes(monkeypatch) -> None:
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.evidence_compose._try_read_image_artifact",
        lambda row, artifact, media_type: {
            "type": "image",
            "media_type": "image/jpeg",
            "data": "Zm9v",
        },
    )
    parts = compose_evidence_parts(
        {
            "chunk_type": "image",
            "content": "chart caption",
            "file_path": "images/a.jpg",
            "job_id": "job-1",
        },
        {},
    )
    assert parts[0] == {"type": "text", "text": "chart caption"}
    assert parts[1] == {"type": "image", "media_type": "image/jpeg", "data": "Zm9v"}


@pytest.mark.asyncio
async def test_text_result_keeps_placeholders_and_composes_assets(monkeypatch) -> None:
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.evidence_compose._try_read_image_artifact",
        lambda row, artifact, media_type: {
            "type": "image",
            "media_type": "image/png",
            "data": "aW1n",
        },
    )
    assembled = await assemble_retrieval_results(
        rows=[
            {
                "chunk_id": "text-1",
                "chunk_type": "text",
                "content": "见表 [tables/a.html] 再看 [images/a.png] 结束",
                "chunk_metadata": {
                    "connect_to": [
                        {
                            "target": "table-1",
                            "relation": "embeds",
                            "ref": "[tables/a.html]",
                        },
                        {
                            "target": "image-1",
                            "relation": "embeds",
                            "ref": "[images/a.png]",
                        },
                    ]
                },
            },
            {
                "chunk_id": "table-1",
                "chunk_type": "table",
                "content": "<table><tr><td>Q4</td></tr></table>",
                "file_path": "tables/a.html",
            },
            {
                "chunk_id": "image-1",
                "chunk_type": "image",
                "file_path": "images/a.png",
                "job_id": "job-1",
            },
        ],
        exclude_document_ids=[],
        exclude_sections=[],
    )
    assert assembled[0]["content"] == "见表 [tables/a.html] 再看 [images/a.png] 结束"
    types = [part["type"] for part in assembled[0]["composed"]]
    assert "image" in types
    composed_text = "".join(
        part["text"] for part in assembled[0]["composed"] if part["type"] == "text"
    )
    assert "<table><tr><td>Q4</td></tr></table>" in composed_text
    assert "[tables/" not in composed_text
    assert "[images/" not in composed_text


def test_text_image_keeps_newlines_around_image(monkeypatch) -> None:
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.evidence_compose._try_read_image_artifact",
        lambda row, artifact, media_type: {
            "type": "image",
            "media_type": "image/png",
            "data": "aW1n",
        },
    )
    parts = compose_evidence_parts(
        {
            "chunk_type": "text",
            "content": "前 [images/a.png] 后",
            "chunk_metadata": {
                "connect_to": [
                    {
                        "target": "image-1",
                        "relation": "embeds",
                        "ref": "[images/a.png]",
                    }
                ]
            },
        },
        {
            "image-1": {
                "chunk_type": "image",
                "file_path": "images/a.png",
                "job_id": "job-1",
            }
        },
    )
    assert parts[0] == {"type": "text", "text": "前 \n"}
    assert parts[1] == {"type": "image", "media_type": "image/png", "data": "aW1n"}
    assert parts[2] == {"type": "text", "text": "\n"}
    assert parts[3] == {"type": "text", "text": " 后"}


def test_unreachable_assets_clear_placeholders(monkeypatch) -> None:
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.evidence_compose._try_read_image_artifact",
        lambda row, artifact, media_type: None,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.evidence_compose._try_read_table_html",
        lambda row: None,
    )
    parts = compose_evidence_parts(
        {
            "chunk_type": "text",
            "content": "见表 [tables/a.html] 再看 [images/a.png] 结束",
            "chunk_metadata": {
                "connect_to": [
                    {
                        "target": "table-1",
                        "relation": "embeds",
                        "ref": "[tables/a.html]",
                    },
                    {
                        "target": "image-1",
                        "relation": "embeds",
                        "ref": "[images/a.png]",
                    },
                ]
            },
        },
        {
            "table-1": {"chunk_type": "table", "file_path": "tables/a.html"},
            "image-1": {
                "chunk_type": "image",
                "file_path": "images/a.png",
                "job_id": "job-1",
            },
        },
    )
    composed_text = "".join(part["text"] for part in parts if part["type"] == "text")
    assert composed_text == "见表  再看  结束"
    assert all(part["type"] != "image" for part in parts)


def test_missing_placeholder_does_not_append_asset(monkeypatch) -> None:
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.evidence_compose._try_read_image_artifact",
        lambda row, artifact, media_type: {
            "type": "image",
            "media_type": "image/png",
            "data": "aW1n",
        },
    )
    parts = compose_evidence_parts(
        {
            "chunk_type": "text",
            "content": "没有占位符",
            "chunk_metadata": {
                "connect_to": [
                    {
                        "target": "image-1",
                        "relation": "embeds",
                        "ref": "[images/a.png]",
                    }
                ]
            },
        },
        {
            "image-1": {
                "chunk_type": "image",
                "file_path": "images/a.png",
                "job_id": "job-1",
            }
        },
    )
    assert parts == [{"type": "text", "text": "没有占位符"}]


def test_page_image_requires_matching_page_num(monkeypatch) -> None:
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.evidence_compose._try_read_image_artifact",
        lambda row, artifact, media_type: {
            "type": "image",
            "media_type": media_type,
            "data": "cGFnZQ==",
        },
    )
    parts = compose_evidence_parts(
        {
            "chunk_type": "page",
            "chunk_metadata": {
                "summary": "只要摘要",
                "page_nums": [4],
                "page_assets": [
                    {
                        "page_num": 9,
                        "artifact_ref": "page_citation_assets/page-9.png",
                        "content_type": "image/png",
                    }
                ],
            },
        },
        {},
    )
    assert parts == [{"type": "text", "text": "只要摘要"}]
