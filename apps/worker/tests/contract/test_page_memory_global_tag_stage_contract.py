"""Contract tests for document-level page tagging + scope fine hierarchy."""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.page_memory._serialization import (
    deserialize_page_tags,
    deserialize_scope_skeletons,
    load_page_tags_payload,
    serialize_page_tags,
    serialize_scope_skeletons,
)
from app.services.page_memory._utils import build_hierarchy_scopes
from app.services.page_memory.memory_service import (
    _extract_document_assets,
    _merge_assets_by_page,
    _page_to_node_map,
    _project_assets_for_pages,
    _render_and_tag_document_pages,
    _resolve_assembly_page_text,
    _select_rendered_pages_with_assets,
    _union_scope_processing_pages,
)
from app.services.page_memory.page_assets import PageAsset
from app.services.page_memory.page_renderer import PageRenderResult
from app.services.page_memory.page_tagger import PageTagResult
from app.services.page_memory.skeleton_extractor import SectionSkeleton
from app.services.page_memory.toc_page_policy import TocPagePolicy
from shared.models.schemas.page_memory_config import PageMemoryConfig
from shared.services.ai.prompt_service import build_prompt
from types import SimpleNamespace


def _skel(
    *,
    title: str,
    start: int,
    end: int,
    parent: str = "doc.pdf",
) -> SectionSkeleton:
    return SectionSkeleton(
        section_path=f"{parent}/{title}",
        level=1,
        start_page=start,
        end_page=end,
        title=title,
        parent_path=parent,
    )


def test_union_processing_pages_dedups_shared_boundary_and_excludes_toc() -> None:
    policy = TocPagePolicy(
        pure_toc_pages=frozenset({3, 4, 5, 6, 7, 8}),
        mixed_boundary_by_page={},
        regions=(),
    )
    scopes = build_hierarchy_scopes(
        skeletons=[
            _skel(title="Copyright", start=2, end=9),
            _skel(title="Intro", start=9, end=11),
        ],
        filename="doc.pdf",
        page_count=12,
        processing_pages=policy.filter_processing_pages(list(range(1, 13))),
        excluded_toc_pages=sorted(policy.pure_toc_pages),
    )

    pages = _union_scope_processing_pages(scopes)

    assert pages == [2, 9, 10, 11]
    assert pages.count(9) == 1


def test_render_and_tag_calls_once_per_processing_page(monkeypatch, tmp_path) -> None:
    tagged: list[int] = []

    def _fake_render(**kwargs):
        return [
            PageRenderResult(
                page_index=page,
                image_path=str(tmp_path / f"p{page}.png"),
                raw_text="",
                width=10,
                height=10,
                is_landscape=False,
            )
            for page in kwargs["pages"]
        ]

    def _fake_tag_pages(*, pages, max_concurrent=None, **_kwargs):
        assert max_concurrent == 5
        for page in pages:
            tagged.append(page.page_index)
        return [
            PageTagResult(page_index=page.page_index, strategy_used="vlm_page")
            for page in pages
        ]

    monkeypatch.setattr(
        "app.services.page_memory.page_renderer.render_document_pages",
        _fake_render,
    )
    monkeypatch.setattr(
        "app.services.page_memory.page_plan.derive_page_processing_plan",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        "app.services.page_memory.page_tagger.tag_pages",
        _fake_tag_pages,
    )

    rendered_by_page, tags_by_page = _render_and_tag_document_pages(
        pdf_path=str(tmp_path / "doc.pdf"),
        output_dir=str(tmp_path),
        page_count=20,
        processing_pages=[9, 10, 11],
        page_texts={},
        page_features=[],
        page_labels=[],
        vlm_model="fake",
        toc_policy=TocPagePolicy(frozenset(), {}, ()),
        page_memory_config=PageMemoryConfig(tag_concurrency=5),
    )

    assert tagged == [9, 10, 11]
    assert sorted(rendered_by_page) == [9, 10, 11]
    assert sorted(tags_by_page) == [9, 10, 11]


def test_page_prompt_has_no_coarse_scope_context() -> None:
    prompt, *_ = build_prompt(
        "page-memory-vlm-page",
        "",
        "",
        paras={"max_tokens": 800, "scan_direction": "top_to_bottom_left_to_right"},
    )
    assert "Confirmed coarse parent section" not in prompt


def test_hierarchy_prompt_keeps_coarse_title_path_pages() -> None:
    prompt, *_ = build_prompt(
        "page-memory-hierarchy",
        '[{"id": 1, "page": 10, "heading": "A.1"}]',
        "",
        paras={
            "max_depth": 6,
            "max_tokens": 2000,
            "coarse_context": "title=Section A\npath=doc/Section A\npages=10-20",
        },
    )
    assert "Confirmed coarse parent section" in prompt
    assert "title=Section A" in prompt
    assert "path=doc/Section A" in prompt
    assert "pages=10-20" in prompt


def test_scope_skeletons_artifact_roundtrips_processing_pages(tmp_path: Path) -> None:
    payload = serialize_scope_skeletons(
        scope_id="p2-9",
        start_page=2,
        end_page=9,
        strategy="leaf_scope",
        skeletons=[_skel(title="Copyright", start=2, end=9)],
        processing_pages=[2, 9],
        excluded_toc_pages=[3, 4, 5, 6, 7, 8],
    )
    path = tmp_path / "skeletons.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["processing_pages"] == [2, 9]
    assert data["excluded_toc_pages"] == [3, 4, 5, 6, 7, 8]
    assert data["skeletons"][0]["title"] == "Copyright"


def test_debug_loaders_roundtrip_production_artifacts(tmp_path: Path) -> None:
    skeleton_payload = serialize_scope_skeletons(
        scope_id="p2-9",
        start_page=2,
        end_page=9,
        strategy="leaf_scope",
        skeletons=[_skel(title="Copyright", start=2, end=9)],
        processing_pages=[2, 9],
        excluded_toc_pages=[3, 4, 5, 6, 7, 8],
    )
    meta, skeletons = deserialize_scope_skeletons(skeleton_payload)
    _mode, tags = deserialize_page_tags(
        serialize_page_tags(
            [
                PageTagResult(
                    page_index=2,
                    summary="summary",
                    strategy_used="text_page",
                    tagging_mode="text",
                )
            ],
            tagging_mode="text",
        )
    )

    assert meta["processing_pages"] == [2, 9]
    assert meta["excluded_toc_pages"] == [3, 4, 5, 6, 7, 8]
    assert skeletons[0].title == "Copyright"
    assert tags[0].summary == "summary"
    assert tags[0].tagging_mode == "text"


def test_page_tags_payload_loader_supports_v2_and_legacy() -> None:
    mode, tags = load_page_tags_payload(
        serialize_page_tags(
            [PageTagResult(page_index=1, tagging_mode="text")],
            tagging_mode="text",
        )
    )
    legacy_mode, legacy_tags = load_page_tags_payload([{"page_index": 1}])

    assert mode == "text"
    assert tags[0]["tagging_mode"] == "text"
    assert legacy_mode == "visual"
    assert legacy_tags == [{"page_index": 1}]


def test_node_assembly_reuses_transient_tagging_ocr(tmp_path: Path) -> None:
    rendered = PageRenderResult(
        page_index=1,
        image_path=str(tmp_path / "page-1.png"),
        raw_text="",
        width=100,
        height=200,
        is_landscape=False,
    )
    tag = PageTagResult(
        page_index=1,
        tagging_mode="text",
        resolved_body_text="ocr body",
    )

    assert (
        _resolve_assembly_page_text(
            rendered=rendered,
            tag=tag,
            fallback_text="",
        )
        == "ocr body"
    )


def _page_render(page: int, tmp_path: Path) -> PageRenderResult:
    return PageRenderResult(
        page_index=page,
        image_path=str(tmp_path / f"p{page}.png"),
        raw_text=f"text-{page}",
        width=10,
        height=10,
        is_landscape=False,
    )


def test_extract_document_assets_filters_has_asset_and_calls_once(
    monkeypatch, tmp_path
) -> None:
    extract_calls: list[list[int]] = []

    def _fake_extract(**kwargs):
        pages = [item.page_index for item in kwargs["rendered_pages"]]
        extract_calls.append(pages)
        assert kwargs["max_pages"] == 2
        return {
            pages[0]: [
                PageAsset(
                    asset_id="asset_a",
                    page_index=pages[0],
                    asset_index=1,
                    kind="table",
                    bbox_px=[0, 0, 1, 1],
                    width_px=10,
                    height_px=10,
                    width_pt=1.0,
                    height_pt=1.0,
                    html_uri=f"tables/t{pages[0]}.html",
                )
            ]
        }

    monkeypatch.setattr(
        "app.services.page_memory.page_assets.extract_page_assets_from_renders",
        _fake_extract,
    )

    rendered_by_page = {
        9: _page_render(9, tmp_path),
        10: _page_render(10, tmp_path),
        11: _page_render(11, tmp_path),
    }
    assets = _extract_document_assets(
        pdf_path=str(tmp_path / "doc.pdf"),
        output_dir=str(tmp_path),
        rendered_by_page=rendered_by_page,
        page_features=[
            SimpleNamespace(page=9, has_asset=False),
            SimpleNamespace(page=10, has_asset=True),
            SimpleNamespace(page=11, has_asset=True),
        ],
        page_count=20,
        page_memory_config=PageMemoryConfig(asset_max_pages=2),
    )

    assert extract_calls == [[10, 11]]
    assert sorted(assets) == [10]


def test_shared_boundary_page_projected_without_duplicate_ids() -> None:
    shared = PageAsset(
        asset_id="asset_shared",
        page_index=153,
        asset_index=1,
        kind="table",
        bbox_px=[0, 0, 1, 1],
        width_px=10,
        height_px=10,
        width_pt=1.0,
        height_pt=1.0,
        html_uri="tables/t153.html",
        source_page_nums=[153],
    )
    document_assets = {153: [shared]}

    left = _project_assets_for_pages(document_assets, {149, 150, 151, 152, 153})
    right = _project_assets_for_pages(document_assets, {153, 154, 155})
    merged = _merge_assets_by_page([left, right])

    assert list(left) == [153]
    assert list(right) == [153]
    assert len(merged[153]) == 1
    assert merged[153][0].asset_id == "asset_shared"


def test_page_to_node_map_uses_owned_pages_only() -> None:
    mapping = _page_to_node_map(
        [
            {
                "chunk_id": "owner",
                "type": "page",
                "content": "owned",
                "path": "doc.pdf/Owner",
                "metadata": {
                    "page_nums": [1, 2],
                    "owned_page_nums": [1],
                    "connect_to": [],
                },
                "order": 0,
            },
            {
                "chunk_id": "alias",
                "type": "page",
                "content": "alias",
                "path": "doc.pdf/Alias",
                "metadata": {
                    "page_nums": [1, 2],
                    "owned_page_nums": [2],
                    "connect_to": [],
                },
                "order": 1,
            },
        ]
    )

    assert mapping == {1: "doc.pdf/Owner", 2: "doc.pdf/Alias"}


def test_select_rendered_pages_with_assets_keeps_only_has_asset_pages() -> None:
    rendered = [
        _page_render(1, Path("/tmp")),
        _page_render(2, Path("/tmp")),
        _page_render(3, Path("/tmp")),
    ]
    page_features = [
        SimpleNamespace(page=1, has_asset=False),
        SimpleNamespace(page=2, has_asset=True),
        SimpleNamespace(page=3, has_asset=True),
    ]

    selected = _select_rendered_pages_with_assets(rendered, page_features)

    assert [item.page_index for item in selected] == [2, 3]

