"""Unit tests for placeholder-based asset inlining."""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from shared.services.retrieval.hydration.asset_inline import (
    inline_assets_at_placeholders,
    remove_path_placeholders,
)
from shared.services.retrieval.hydration.result_assembly import (
    assemble_retrieval_results,
)
from shared.services.storage.result_storage import JobResultStorage
from shared.services.retrieval.scoring.hierarchy import ProviderToolSpace
from shared.services.retrieval.scoring.knowhere_provider import (
    KnowhereProvider,
    SectionRow,
    UnitRow,
)


def test_remove_path_placeholders_requires_asset_extension() -> None:
    leftover = (
        "前 [images/image-3-适应证_(1)二级预防_患者 $^{[99]}$ (I,A)。(2)一级.jpg] 后"
    )
    assert remove_path_placeholders(leftover) == "前  后"
    assert remove_path_placeholders("见表 [tables/table-1.html] 完") == "见表  完"
    assert remove_path_placeholders("不是引用 [images/foo] 也不是 [tables/a]") == (
        "不是引用 [images/foo] 也不是 [tables/a]"
    )


def test_inline_replaces_placeholder_with_newlines() -> None:
    body, embedded = inline_assets_at_placeholders(
        "see [images/a.png] here",
        connections=[
            {
                "target": "img-1",
                "relation": "embeds",
                "ref": "[images/a.png]",
            }
        ],
        display_by_target={"img-1": "[Image: images/a.png]\nsummary"},
    )
    assert body == "see \n[Image: images/a.png]\nsummary\n here"
    assert embedded == {"img-1"}
    assert "[images/" not in body


def test_inline_appends_when_placeholder_missing() -> None:
    body, embedded = inline_assets_at_placeholders(
        "plain text",
        connections=[{"target": "img-1", "ref": "[images/a.png]"}],
        display_by_target={"img-1": "[Image: images/a.png]"},
    )
    assert body == "plain text\n\n[Image: images/a.png]"
    assert embedded == {"img-1"}


def test_inline_does_not_duplicate_target() -> None:
    body, embedded = inline_assets_at_placeholders(
        "x [images/a.png] y",
        connections=[
            {"target": "img-1", "ref": "[images/a.png]"},
            {"target": "img-1", "ref": "[images/a.png]"},
        ],
        display_by_target={"img-1": "[Image: images/a.png]"},
    )
    assert body.count("[Image: images/a.png]") == 1
    assert embedded == {"img-1"}


@pytest.mark.asyncio
async def test_assemble_inserts_table_at_placeholder() -> None:
    rows = [
        {
            "chunk_id": "text-1",
            "chunk_type": "text",
            "content": "见表 [tables/table-1.html] 结束",
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
                "keywords": ["企业名称"],
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
    assert "[tables/table-1.html]" in content
    assert "[Table:" not in content
    composed_text = "".join(
        str(part.get("text") or "")
        for part in assembled[0]["composed"]
        if part.get("type") == "text"
    )
    assert composed_text.index("见表") < composed_text.index("<table")
    assert composed_text.index("<table") < composed_text.index("结束")
    assert "SHOULD NOT LEAK" in composed_text
    assert "[tables/" not in composed_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asset_type", "file_path", "placeholder", "display_marker"),
    [
        ("image", "images/a.png", "[images/a.png]", "[Image: images/a.png]"),
        ("table", "tables/a.html", "[tables/a.html]", "[Table: tables/a.html]"),
    ],
)
async def test_asset_type_filter_keeps_body_that_connects_to_requested_asset(
    monkeypatch,
    asset_type: str,
    file_path: str,
    placeholder: str,
    display_marker: str,
) -> None:
    body_row = {
        "chunk_id": "text-1",
        "chunk_type": "text",
        "content": f"查看 {placeholder}",
        "chunk_metadata": {
            "connect_to": [
                {
                    "target": "asset-1",
                    "relation": "embeds",
                    "ref": placeholder,
                }
            ]
        },
    }
    asset_row = {
        "chunk_id": "asset-1",
        "chunk_type": asset_type,
        "content": "资产说明" if asset_type == "image" else "<table></table>",
        "file_path": file_path,
        "chunk_metadata": {"summary": "资产说明"},
    }

    async def hydrate_connected_rows(**_kwargs: object) -> list[dict[str, object]]:
        return [asset_row]

    monkeypatch.setattr(
        "shared.services.retrieval.hydration.result_assembly.hydrate_connected_target_rows",
        hydrate_connected_rows,
    )

    assembled = await assemble_retrieval_results(
        rows=[
            body_row,
            {"chunk_id": "text-2", "chunk_type": "text", "content": "无图"},
        ],
        exclude_document_ids=[],
        exclude_sections=[],
        allowed_chunk_types={asset_type},
    )

    assert [row["chunk_id"] for row in assembled] == ["text-1"]
    assert placeholder in assembled[0]["content"]
    assert display_marker not in assembled[0]["content"]


@pytest.mark.asyncio
async def test_assemble_skips_connected_image_without_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_factory = Mock()
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.evidence_compose.get_result_storage",
        storage_factory,
    )
    rows = [
        {
            "chunk_id": "text-1",
            "chunk_type": "text",
            "content": "The process is illustrated below.",
            "chunk_metadata": {
                "connect_to": [
                    {
                        "target": "image-1",
                        "relation": "embeds",
                        "ref": "[images/flow.png]",
                    }
                ]
            },
        },
        {
            "chunk_id": "image-1",
            "chunk_type": "image",
            "content": "Flowchart of the ingestion pipeline.",
            "file_path": "images/flow.png",
            "job_id": "job-synth",
        },
    ]
    assembled = await assemble_retrieval_results(
        rows=rows,
        exclude_document_ids=[],
        exclude_sections=[],
    )

    assert [row["chunk_id"] for row in assembled] == ["text-1"]
    assert assembled[0]["content"] == rows[0]["content"]
    assert assembled[0]["composed"] == [
        {"type": "text", "text": "The process is illustrated below."}
    ]
    storage_factory.assert_not_called()


@pytest.mark.asyncio
async def test_assemble_inserts_image_at_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4"
        "DwABBAEAX+XDSwAAAABJRU5ErkJggg=="
    )
    image_path = tmp_path / "a.png"
    image_path.write_bytes(image_bytes)
    storage = Mock(spec=JobResultStorage)
    storage.normalize_artifact_ref.return_value = "images/a.png"
    storage.download_raw_to_temp.return_value = str(image_path)
    monkeypatch.setattr(
        "shared.services.retrieval.hydration.evidence_compose.get_result_storage",
        lambda: storage,
    )
    rows = [
        {
            "chunk_id": "text-1",
            "chunk_type": "text",
            "content": "see [images/a.png] end",
            "chunk_metadata": {
                "connect_to": [
                    {
                        "target": "img-1",
                        "relation": "embeds",
                        "ref": "[images/a.png]",
                    }
                ]
            },
        },
        {
            "chunk_id": "img-1",
            "chunk_type": "image",
            "content": "chart summary",
            "file_path": "images/a.png",
            "job_id": "job-synth",
        },
    ]
    assembled = await assemble_retrieval_results(
        rows=rows,
        exclude_document_ids=[],
        exclude_sections=[],
    )

    assert [row["chunk_id"] for row in assembled] == ["text-1"]
    assert assembled[0]["content"] == "see [images/a.png] end"
    assert assembled[0]["composed"] == [
        {"type": "text", "text": "see \n"},
        {
            "type": "image",
            "media_type": "image/png",
            "data": base64.b64encode(image_bytes).decode("ascii"),
        },
        {"type": "text", "text": "\n"},
        {"type": "text", "text": " end"},
    ]
    storage.download_raw_to_temp.assert_called_once_with(
        job_id="job-synth",
        relative_path="images/a.png",
        suffix=".png",
        temp_dir=tempfile.gettempdir(),
    )


def test_node_unit_span_inlines_section_assets() -> None:
    provider = KnowhereProvider(
        doc_id="doc-1",
        sections=[
            SectionRow(
                section_id="sec-1",
                parent_section_id=None,
                section_path="One",
                section_title="One",
                section_level=1,
                summary="",
                sort_order=0,
            )
        ],
        units=[
            UnitRow(
                chunk_id="text-1",
                section_id="sec-1",
                chunk_type="text",
                content="see [images/a.png] end",
                sort_order=0,
                metadata={
                    "connect_to": [
                        {
                            "target": "img-1",
                            "relation": "embeds",
                            "ref": "[images/a.png]",
                        }
                    ]
                },
            ),
            UnitRow(
                chunk_id="img-1",
                section_id="sec-1",
                chunk_type="image",
                content="images/a.png",
                sort_order=1,
                file_path="images/a.png",
                metadata={"summary": "chart summary"},
            ),
        ],
    )
    ts = ProviderToolSpace(provider)
    text, _order, count = ts._node_unit_span("sec-1")
    assert count == 2
    assert "[images/" not in text
    assert text.index("see") < text.index("[Image:")
    assert text.index("[Image:") < text.index("end")
    assert "chart summary" in text
    # Asset must not also appear as a trailing standalone copy.
    assert text.count("[Image:") == 1
