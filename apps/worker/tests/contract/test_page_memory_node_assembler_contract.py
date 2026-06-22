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
from app.services.page_memory.page_tagger import PageTagResult
from app.services.page_memory.skeleton_extractor import SectionSkeleton


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
        image_uri_by_page={231: "pages/page-231.png", 232: "pages/page-232.png"},
        image_path_by_page={},
        kind_by_page={},
        tag_by_page={
            231: PageTagResult(page_index=231, summary="s231", keywords=["k1"]),
            232: PageTagResult(page_index=232, summary="s232", keywords=["k2"]),
        },
        filename="demo.pdf",
        verdict="page",
        native_hierarchy=True,
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
    assert leaf_a["extra_metadata"]["page_image_uris"] == ["pages/page-231.png"]
    assert leaf_a["extra_metadata"]["granularity"] == "node"

    leaf_b = by_path["demo.pdf/3 基本规定/3.2 管理规定"]
    assert leaf_b["page_nums"] == "231,232"
    assert SAME_AS_PREFIX in leaf_b["content"]
    assert "text-232" in leaf_b["content"]
    assert leaf_b["extra_metadata"]["page_image_uris"] == [
        "pages/page-231.png",
        "pages/page-232.png",
    ]
    assert leaf_b["extra_metadata"]["owned_pages"] == [232]


def test_build_node_rows_uses_vlm_node_summary_with_boundary(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    class _FakeClient:
        def chat_completion_with_usage(self, **kwargs):
            captured["messages"] = kwargs.get("messages")
            captured["usage_task"] = kwargs.get("usage_task")
            return ('{"summary": "node summary", "keywords": "ka;kb"}', {"total_tokens": 10})

    # node_assembler imports get_openai_client lazily from this module.
    import shared.services.ai.openai_compatible_client_sync as client_mod

    monkeypatch.setattr(client_mod, "get_openai_client", lambda model=None: _FakeClient())

    img = tmp_path / "page-231.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n fake")

    rows = build_node_rows(
        skeletons=_same_page_sibling_skeletons(),
        raw_text_by_page={231: "text-231", 232: "text-232"},
        image_uri_by_page={231: "pages/page-231.png", 232: "pages/page-232.png"},
        image_path_by_page={231: str(img), 232: str(img)},
        kind_by_page={},
        tag_by_page={
            231: PageTagResult(page_index=231, summary="s231", keywords=["k1"]),
            232: PageTagResult(page_index=232, summary="s232", keywords=["k2"]),
        },
        filename="demo.pdf",
        verdict="page",
        native_hierarchy=True,
        budget=None,
        vlm_model="fake-vlm",
    )

    by_path = {r["path"]: r for r in rows}
    leaf_a = by_path["demo.pdf/3 基本规定/3.1 职责"]
    assert leaf_a["summary"] == "node summary"
    assert leaf_a["keywords"] == "ka;kb"
    assert captured["usage_task"] == "page_memory.node_summary"
