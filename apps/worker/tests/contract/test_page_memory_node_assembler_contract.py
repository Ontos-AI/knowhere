from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.page_memory.node_assembler import (
    SAME_AS_PREFIX,
    assign_pages_to_leaves,
    build_node_content,
    build_node_rows,
    identify_leaf_nodes,
)
from app.services.document_parser.support.parser_rows import PARSER_ROW_COLUMNS
from app.services.page_memory.page_assets import PageAsset
from app.services.page_memory.page_tagger import PageTagResult
from app.services.page_memory.skeleton_extractor import SectionSkeleton
from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks

import pandas as pd


def _same_page_sibling_skeletons() -> list[SectionSkeleton]:
    parent = SectionSkeleton(
        section_path="demo.pdf/3 基本规定",
        level=1,
        start_page=231,
        end_page=232,
        title="3 基本规定",
        parent_path="demo.pdf",
    )
    child_a = SectionSkeleton(
        section_path="demo.pdf/3 基本规定/3.1 职责",
        level=2,
        start_page=231,
        end_page=231,
        title="3.1 职责",
        parent_path="demo.pdf/3 基本规定",
    )
    child_b = SectionSkeleton(
        section_path="demo.pdf/3 基本规定/3.2 管理规定",
        level=2,
        start_page=231,
        end_page=232,
        title="3.2 管理规定",
        parent_path="demo.pdf/3 基本规定",
    )
    return [parent, child_a, child_b]


def test_identify_leaf_nodes_drops_internal_parents() -> None:
    leaves = identify_leaf_nodes(_same_page_sibling_skeletons())
    assert [leaf.title for leaf in leaves] == ["3.1 职责", "3.2 管理规定"]


def test_page_ownership_first_leaf_owns_shared_page() -> None:
    leaves = identify_leaf_nodes(_same_page_sibling_skeletons())
    views, page_owner = assign_pages_to_leaves(leaves, available_pages={231, 232})

    assert page_owner[231].title == "3.1 职责"
    assert page_owner[232].title == "3.2 管理规定"

    by_title = {view.leaf.title: view for view in views}
    assert by_title["3.1 职责"].owned_pages == [231]
    assert by_title["3.2 管理规定"].owned_pages == [232]
    assert by_title["3.2 管理规定"].pages == [231, 232]


def test_build_node_content_uses_same_as_for_shared_page() -> None:
    leaves = identify_leaf_nodes(_same_page_sibling_skeletons())
    views, page_owner = assign_pages_to_leaves(leaves, available_pages={231, 232})
    by_title = {view.leaf.title: view for view in views}
    page_text = {231: "text-231", 232: "text-232"}

    content_a = build_node_content(
        by_title["3.1 职责"], page_owner=page_owner, page_text=page_text
    )
    content_b = build_node_content(
        by_title["3.2 管理规定"], page_owner=page_owner, page_text=page_text
    )

    assert content_a == "text-231"
    assert content_b.startswith(f"[{SAME_AS_PREFIX} demo.pdf/3 基本规定/3.1 职责 p231]")
    assert "text-232" in content_b
    assert "text-231" not in content_b


def test_build_node_rows_reuses_tags_without_vlm() -> None:
    rows = build_node_rows(
        skeletons=_same_page_sibling_skeletons(),
        raw_text_by_page={231: "text-231", 232: "text-232"},
        image_path_by_page={},
        kind_by_page={},
        tag_by_page={
            231: PageTagResult(page_index=231, summary="s231", keywords=["k1"]),
            232: PageTagResult(page_index=232, summary="s232", keywords=["k2"]),
        },
        filename="demo.pdf",
        verdict="page",
        budget=None,
        vlm_model=None,
    )

    assert [r["path"] for r in rows] == [
        "demo.pdf/3 基本规定/3.1 职责",
        "demo.pdf/3 基本规定/3.2 管理规定",
    ]
    by_path = {r["path"]: r for r in rows}

    leaf_a = by_path["demo.pdf/3 基本规定/3.1 职责"]
    assert leaf_a["page_nums"] == "231"
    assert leaf_a["content"] == "text-231"
    assert leaf_a["extra_metadata"] == {}

    leaf_b = by_path["demo.pdf/3 基本规定/3.2 管理规定"]
    assert leaf_b["page_nums"] == "231,232"
    assert SAME_AS_PREFIX in leaf_b["content"]
    assert "text-232" in leaf_b["content"]
    assert leaf_b["extra_metadata"] == {}


def test_build_node_rows_keeps_internal_section_body_pages() -> None:
    parent = SectionSkeleton(
        section_path="demo.pdf/4 风险辨识与分级管控",
        level=1,
        start_page=233,
        end_page=234,
        title="4 风险辨识与分级管控",
        parent_path="demo.pdf",
    )
    child = SectionSkeleton(
        section_path="demo.pdf/4 风险辨识与分级管控/4.1 风险评价方法",
        level=2,
        start_page=234,
        end_page=234,
        title="4.1 风险评价方法",
        parent_path="demo.pdf/4 风险辨识与分级管控",
    )

    rows = build_node_rows(
        skeletons=[parent, child],
        raw_text_by_page={233: "parent body", 234: "child body"},
        image_path_by_page={},
        kind_by_page={},
        tag_by_page={
            233: PageTagResult(page_index=233, summary="s233", keywords=["parent"]),
            234: PageTagResult(page_index=234, summary="s234", keywords=["child"]),
        },
        filename="demo.pdf",
        verdict="page",
        budget=None,
        vlm_model=None,
    )

    by_path = {row["path"]: row for row in rows}
    assert by_path["demo.pdf/4 风险辨识与分级管控"]["page_nums"] == "233"
    assert by_path["demo.pdf/4 风险辨识与分级管控"]["content"] == "parent body"
    assert (
        by_path["demo.pdf/4 风险辨识与分级管控/4.1 风险评价方法"]["page_nums"]
        == "234"
    )
    assert (
        by_path["demo.pdf/4 风险辨识与分级管控/4.1 风险评价方法"]["content"]
        == "child body"
    )


def test_boundary_page_belongs_to_next_sibling_start() -> None:
    coarse = SectionSkeleton(
        section_path="demo.pdf/安全类/风险标准",
        level=3,
        start_page=225,
        end_page=302,
        title="风险标准",
        parent_path="demo.pdf/安全类",
    )
    next_sibling = SectionSkeleton(
        section_path="demo.pdf/安全类/项目分类标准",
        level=3,
        start_page=302,
        end_page=304,
        title="项目分类标准",
        parent_path="demo.pdf/安全类",
    )

    leaves = identify_leaf_nodes([coarse, next_sibling])
    views, page_owner = assign_pages_to_leaves(
        leaves,
        available_pages={301, 302, 303},
    )

    by_title = {view.leaf.title: view for view in views}
    assert by_title["风险标准"].pages == [301]
    assert by_title["项目分类标准"].pages == [302, 303]
    assert page_owner[301].title == "风险标准"
    assert page_owner[302].title == "项目分类标准"


def test_build_node_rows_uses_vlm_node_summary_with_boundary(
    monkeypatch, tmp_path
) -> None:
    captured: dict[str, object] = {}

    class _FakeClient:
        def chat_completion_with_usage(self, **kwargs):
            captured["messages"] = kwargs.get("messages")
            captured["usage_task"] = kwargs.get("usage_task")
            return (
                '{"summary": "node summary", "keywords": "ka;kb"}',
                {"total_tokens": 10},
            )

    # node_assembler imports get_openai_client lazily from this module.
    import shared.services.ai.openai_compatible_client_sync as client_mod

    monkeypatch.setattr(
        client_mod, "get_openai_client", lambda model=None: _FakeClient()
    )

    img = tmp_path / "page-231.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n fake")

    rows = build_node_rows(
        skeletons=_same_page_sibling_skeletons(),
        raw_text_by_page={231: "text-231", 232: "text-232"},
        image_path_by_page={231: str(img), 232: str(img)},
        kind_by_page={},
        tag_by_page={
            231: PageTagResult(page_index=231, summary="s231", keywords=["k1"]),
            232: PageTagResult(page_index=232, summary="s232", keywords=["k2"]),
        },
        filename="demo.pdf",
        verdict="page",
        budget=None,
        vlm_model="fake-vlm",
    )

    by_path = {r["path"]: r for r in rows}
    leaf_a = by_path["demo.pdf/3 基本规定/3.1 职责"]
    assert leaf_a["summary"] == "node summary"
    assert leaf_a["keywords"] == "ka;kb"
    assert captured["usage_task"] == "page_memory.node_summary"


def test_build_node_rows_prepends_asset_rows_and_links_page_nodes() -> None:
    asset = PageAsset(
        asset_id="asset_table_1",
        page_index=231,
        asset_index=1,
        kind="table",
        bbox_px=[10, 20, 200, 120],
        width_px=1000,
        height_px=1400,
        width_pt=500,
        height_pt=700,
        title="资产表",
        summary="表格资产摘要",
        keywords=["资产", "表格"],
        image_uri="images/image_page_231_table_1.png",
        html_uri="tables/table_page_231_1.html",
        extraction_status="table_html_extracted",
    )

    rows = build_node_rows(
        skeletons=_same_page_sibling_skeletons(),
        raw_text_by_page={231: "text-231", 232: "text-232"},
        image_path_by_page={},
        kind_by_page={},
        tag_by_page={
            231: PageTagResult(page_index=231, summary="s231", keywords=["k1"]),
            232: PageTagResult(page_index=232, summary="s232", keywords=["k2"]),
        },
        filename="demo.pdf",
        verdict="page",
        budget=None,
        vlm_model=None,
        page_assets_by_page={231: [asset]},
    )

    assert [row["type"] for row in rows] == ["table", "page", "page"]
    assert rows[0]["path"] == "tables/table_page_231_1.html"
    assert rows[0]["content"] == "tables/table_page_231_1.html"
    assert rows[0]["know_id"] == "asset_table_1"
    assert "owner_hierarchy_path" not in rows[0]["extra_metadata"]
    assert "related_hierarchy_paths" not in rows[0]["extra_metadata"]
    assert "page_index" not in rows[0]["extra_metadata"]
    assert "asset_kind" not in rows[0]["extra_metadata"]
    assert "title" not in rows[0]["extra_metadata"]
    assert "html_uri" not in rows[0]["extra_metadata"]

    by_path = {row["path"]: row for row in rows}
    owner = by_path["demo.pdf/3 基本规定/3.1 职责"]
    shared = by_path["demo.pdf/3 基本规定/3.2 管理规定"]
    assert '"relation": "embeds"' in owner["connectto"]
    assert '"target": "tables/table_page_231_1.html"' in owner["connectto"]
    assert '"relation": "related"' in shared["connectto"]
    assert '"same_as_owner": "demo.pdf/3 基本规定/3.1 职责"' in shared["connectto"]


def test_page_connectto_normalizes_to_asset_chunk_id() -> None:
    rows = [
        {
            "content": "tables/table_page_1_1.html",
            "path": "tables/table_page_1_1.html",
            "type": "table",
            "length": 26,
            "keywords": "asset",
            "summary": "asset summary",
            "know_id": "asset_table_1",
            "tokens": "",
            "connectto": "",
            "addtime": "",
            "page_nums": "1",
            "extra_metadata": {},
        },
        {
            "content": "page text",
            "path": "demo.pdf/Section",
            "type": "page",
            "length": 9,
            "keywords": "",
            "summary": "",
            "know_id": "page_1",
            "tokens": "",
            "connectto": '[{"target":"tables/table_page_1_1.html","relation":"embeds","ref":"[tables/table_page_1_1.html]"}]',
            "addtime": "",
            "page_nums": "1",
            "extra_metadata": {},
        },
    ]
    df = pd.DataFrame(rows, columns=pd.Index([*PARSER_ROW_COLUMNS, "extra_metadata"]))

    chunks = dataframe_to_chunks(df)

    asset_chunk = next(chunk for chunk in chunks if chunk["type"] == "table")
    assert asset_chunk["chunk_id"] == "asset_table_1"
    assert "know_id" not in asset_chunk
    assert "keywords" not in asset_chunk
    assert "summary" not in asset_chunk
    assert "tokens" not in asset_chunk

    page_chunk = next(chunk for chunk in chunks if chunk["type"] == "page")
    assert page_chunk["metadata"]["connect_to"] == [
        {
            "target": "asset_table_1",
            "relation": "embeds",
            "ref": "[tables/table_page_1_1.html]",
        }
    ]
