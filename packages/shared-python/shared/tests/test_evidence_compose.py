from __future__ import annotations

import base64

import pytest

from shared.services.retrieval.hydration.evidence_compose import (
    compose_evidence_parts,
    group_evidence_units,
)
from shared.services.retrieval.hydration.result_assembly import assemble_retrieval_results


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
    assert parts[0] == {"type": "text", "text": "只要摘要"}
    assert parts[1] == {
        "type": "text",
        "text": "Page image unavailable: missing page image",
    }


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


def test_standalone_table_uses_queried_html() -> None:
    parts = compose_evidence_parts(
        {
            "document_id": "doc_a",
            "chunk_id": "table-1",
            "chunk_type": "table",
            "content": "<table><tr><td>FULL TABLE SHOULD NOT APPEAR</td></tr></table>",
        },
        {},
        queried_tables={
            ("doc_a", "table-1"): "<table><tr><th>Unit</th></tr><tr><td>tablet</td></tr></table>"
        },
    )
    assert parts[0]["type"] == "text"
    assert "tablet" in parts[0]["text"]
    assert "FULL TABLE SHOULD NOT APPEAR" not in parts[0]["text"]


def test_embedded_table_uses_queried_html() -> None:
    parts = compose_evidence_parts(
        {
            "document_id": "doc_a",
            "chunk_id": "text-1",
            "chunk_type": "text",
            "content": "见表 [tables/a.html] 结束",
            "chunk_metadata": {
                "connect_to": [
                    {
                        "target": "table-1",
                        "relation": "embeds",
                        "ref": "[tables/a.html]",
                    }
                ]
            },
        },
        {
            "table-1": {
                "document_id": "doc_a",
                "chunk_id": "table-1",
                "chunk_type": "table",
                "content": "<table><tr><td>FULL TABLE SHOULD NOT APPEAR</td></tr></table>",
            }
        },
        queried_tables={
            ("doc_a", "table-1"): "<table><tr><th>Unit</th></tr><tr><td>tablet</td></tr></table>"
        },
    )
    composed_text = "".join(part["text"] for part in parts if part["type"] == "text")
    assert "tablet" in composed_text
    assert "FULL TABLE SHOULD NOT APPEAR" not in composed_text
    assert "[tables/" not in composed_text


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


@pytest.mark.asyncio
async def test_assemble_uses_queried_subtable_and_keeps_full_without_map() -> None:
    full_html = "<table><tr><td>FULL TABLE SHOULD NOT APPEAR</td></tr></table>"
    sub_html = "<table><tr><th>Unit</th></tr><tr><td>tablet</td></tr></table>"
    rows = [
        {
            "document_id": "doc_a",
            "chunk_id": "table-1",
            "chunk_type": "table",
            "content": full_html,
            "sort_order": 1,
        }
    ]
    queried = await assemble_retrieval_results(
        rows=rows,
        exclude_document_ids=[],
        exclude_sections=[],
        queried_tables={("doc_a", "table-1"): sub_html},
    )
    queried_text = "".join(
        part["text"] for part in queried[0]["composed"] if part["type"] == "text"
    )
    assert "tablet" in queried_text
    assert "FULL TABLE SHOULD NOT APPEAR" not in queried_text

    classic = await assemble_retrieval_results(
        rows=rows,
        exclude_document_ids=[],
        exclude_sections=[],
    )
    classic_text = "".join(
        part["text"] for part in classic[0]["composed"] if part["type"] == "text"
    )
    assert "FULL TABLE SHOULD NOT APPEAR" in classic_text
    assert "tablet" not in classic_text


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


def test_image_placeholder_with_inner_bracket_is_fully_removed(monkeypatch) -> None:
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.evidence_compose._try_read_image_artifact",
        lambda row, artifact, media_type: None,
    )
    placeholder = (
        "[images/image-3-适应证_(1)二级预防_患者 $^{[99]}$ (I,A)。(2)一级.jpg]"
    )
    parts = compose_evidence_parts(
        {
            "chunk_type": "text",
            "content": f"前 {placeholder} 后",
            "chunk_metadata": {
                "connect_to": [
                    {
                        "target": "image-1",
                        "relation": "embeds",
                        "ref": "[images/image-3-适应证_(1)二级预防_患者 $^{[99]",
                    }
                ]
            },
        },
        {
            "image-1": {
                "chunk_type": "image",
                "file_path": "images/image-3-适应证_(1)二级预防_患者 $^{[99]}$ (I,A)。(2)一级.jpg",
                "job_id": "job-1",
            }
        },
    )
    composed_text = "".join(part["text"] for part in parts if part["type"] == "text")
    assert composed_text == "前  后"
    assert "一级.jpg" not in composed_text
    assert all(part["type"] != "image" for part in parts)


def test_unreachable_assets_clear_placeholders(monkeypatch) -> None:
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.evidence_compose._try_read_image_artifact",
        lambda row, artifact, media_type: None,
    )
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.evidence_compose._try_read_table_html",
        lambda row, queried_tables=None: None,
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
    assert parts == [
        {"type": "text", "text": "只要摘要"},
        {
            "type": "text",
            "text": "Page image unavailable: page image does not match this page",
        },
    ]


def test_page_image_missing_page_number_does_not_use_first_asset(monkeypatch) -> None:
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
                "page_assets": [
                    {
                        "page_num": 1,
                        "artifact_ref": "page_citation_assets/page-1.png",
                        "content_type": "image/png",
                    }
                ],
            },
        },
        {},
    )
    assert parts == [
        {"type": "text", "text": "只要摘要"},
        {
            "type": "text",
            "text": "Page image unavailable: missing page number",
        },
    ]


def test_group_merges_same_parent_and_splits_different_parents() -> None:
    image = {"type": "image", "media_type": "image/jpeg", "data": "abc"}
    evidence = group_evidence_units(
        [
            {
                "document_id": "doc_a",
                "source_file_name": "心衰指南.pdf",
                "section_path": "3 诊断 / 3.1",
                "kind": "read",
                "sort_order": 10,
                "parts": [
                    {"type": "text", "text": "...3.1 body..."},
                    image,
                ],
            },
            {
                "document_id": "doc_a",
                "source_file_name": "心衰指南.pdf",
                "section_path": "3 诊断 / 3.2",
                "kind": "read",
                "sort_order": 20,
                "parts": [{"type": "text", "text": "...3.2 body..."}],
            },
            {
                "document_id": "doc_a",
                "source_file_name": "心衰指南.pdf",
                "section_path": "5 治疗 / 5.1",
                "kind": "read",
                "sort_order": 30,
                "parts": [{"type": "text", "text": "...5.1 body..."}],
            },
        ]
    )
    assert evidence == [
        {"type": "text", "text": "[E1] [§ 心衰指南.pdf / 3 诊断]"},
        {"type": "text", "text": "  ...3.1 body..."},
        image,
        {"type": "text", "text": "  ...3.2 body..."},
        {"type": "text", "text": "\n"},
        {"type": "text", "text": "[E2] [§ 心衰指南.pdf / 5 治疗]"},
        {"type": "text", "text": "  ...5.1 body..."},
    ]


def test_group_keeps_single_segment_path() -> None:
    evidence = group_evidence_units(
        [
            {
                "document_id": "doc_a",
                "source_file_name": "guide.pdf",
                "section_path": "3 诊断",
                "kind": "read",
                "sort_order": 1,
                "parts": [{"type": "text", "text": "body"}],
            }
        ]
    )
    assert evidence[0] == {"type": "text", "text": "[E1] [§ guide.pdf / 3 诊断]"}
    assert evidence[1] == {"type": "text", "text": "  body"}


def test_group_orders_by_sort_order_not_input_order() -> None:
    evidence = group_evidence_units(
        [
            {
                "document_id": "doc_a",
                "source_file_name": "guide.pdf",
                "section_path": "5 治疗 / 5.1",
                "kind": "read",
                "sort_order": 30,
                "parts": [{"type": "text", "text": "later"}],
            },
            {
                "document_id": "doc_a",
                "source_file_name": "guide.pdf",
                "section_path": "3 诊断 / 3.1",
                "kind": "read",
                "sort_order": 10,
                "parts": [{"type": "text", "text": "earlier"}],
            },
        ]
    )
    assert [part["text"] for part in evidence if part["type"] == "text"] == [
        "[E1] [§ guide.pdf / 3 诊断]",
        "  earlier",
        "\n",
        "[E2] [§ guide.pdf / 5 治疗]",
        "  later",
    ]


def test_group_merges_page_chunks_under_root() -> None:
    evidence = group_evidence_units(
        [
            {
                "document_id": "doc_a",
                "source_file_name": "slides.pptx",
                "section_path": "Root / Overview (2 of 3)",
                "kind": "read",
                "sort_order": 2,
                "parts": [{"type": "text", "text": "page 2"}],
            },
            {
                "document_id": "doc_a",
                "source_file_name": "slides.pptx",
                "section_path": "Root / Overview (1 of 3)",
                "kind": "read",
                "sort_order": 1,
                "parts": [{"type": "text", "text": "page 1"}],
            },
        ]
    )
    assert evidence == [
        {"type": "text", "text": "[E1] [§ slides.pptx / Root]"},
        {"type": "text", "text": "  page 1"},
        {"type": "text", "text": "  page 2"},
    ]


def test_group_places_outline_before_document_groups() -> None:
    evidence = group_evidence_units(
        [
            {
                "document_id": "doc_b",
                "source_file_name": "other.pdf",
                "section_path": "1 Start / body",
                "sort_order": 1,
                "parts": [{"type": "text", "text": "other body"}],
                "kind": "read",
            },
            {
                "document_id": "doc_a",
                "source_file_name": "guide.pdf",
                "section_path": "",
                "sort_order": None,
                "parts": [{"type": "text", "text": "  [O1] guide.pdf\n  1 Overview"}],
                "kind": "outline",
            },
            {
                "document_id": "doc_a",
                "source_file_name": "guide.pdf",
                "section_path": "2 Treatment",
                "sort_order": 4,
                "parts": [{"type": "text", "text": "treatment body"}],
                "kind": "read",
            },
        ]
    )
    assert evidence == [
        {"type": "text", "text": "[E1] [§ other.pdf / 1 Start]"},
        {"type": "text", "text": "  other body"},
        {"type": "text", "text": "\n"},
        {"type": "text", "text": "[E2] [§ guide.pdf]"},
        {"type": "text", "text": "    [O1] guide.pdf\n    1 Overview"},
        {"type": "text", "text": "\n"},
        {"type": "text", "text": "[E3] [§ guide.pdf / 2 Treatment]"},
        {"type": "text", "text": "  treatment body"},
    ]
