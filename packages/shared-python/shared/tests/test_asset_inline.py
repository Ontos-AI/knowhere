"""Unit tests for placeholder-based asset inlining."""

from __future__ import annotations

import pytest

from shared.services.retrieval.hydration.asset_inline import (
    inline_assets_at_placeholders,
)
from shared.services.retrieval.hydration.result_assembly import (
    assemble_retrieval_results,
)
from shared.services.retrieval.nav.nav_hierarchy import ProviderToolSpace
from shared.services.retrieval.nav.nav_knowhere import (
    KnowhereProvider,
    SectionRow,
    UnitRow,
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
    assert "[tables/" not in content
    assert content.index("见表") < content.index("[Table:")
    assert content.index("[Table:") < content.index("结束")
    assert "企业入驻信息登记模板" in content
    assert "SHOULD NOT LEAK" not in content


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
