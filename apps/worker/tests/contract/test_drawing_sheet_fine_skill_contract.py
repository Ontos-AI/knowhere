from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import importlib

import pytest
from PIL import Image

from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory
from app.services.page_memory.fine_skills import (
    DRAWING_SHEET_SKILL_NAME,
    DrawingSheetFineSkill,
    resolve_active_fine_skill_names,
    resolve_fine_skills,
)
from app.services.page_memory.memory_service import _select_rendered_pages_with_assets
from app.services.page_memory.page_assets import extract_page_assets_from_renders
from app.services.page_memory.page_plan import PagePlan, PageProcessingStrategy
from app.services.page_memory.page_renderer import PageRenderResult
from app.services.page_memory.page_tagger import PageTagResult, tag_page_titles, tag_pages
from app.services.page_memory.phase_a import build_phase_a
from app.services.page_memory.scope_enrichment_service import run_single_scope_refinement
from shared.models.schemas.page_memory_config import PageMemoryConfig
from shared.services.ai.prompt_service import build_prompt
from shared.services.ai.summary.model import BodySummary

_WORKER_SYMBOLS: dict[str, tuple[str, ...]] = {
    "app.services.document_parser.profiling.taxonomy": ("PdfRoutingCategory",),
    "app.services.page_memory.fine_skills": (
        "DRAWING_SHEET_SKILL_NAME",
        "DrawingSheetFineSkill",
        "resolve_active_fine_skill_names",
        "resolve_fine_skills",
    ),
    "app.services.page_memory.memory_service": ("_select_rendered_pages_with_assets",),
    "app.services.page_memory.page_assets": ("extract_page_assets_from_renders",),
    "app.services.page_memory.page_plan": ("PagePlan", "PageProcessingStrategy"),
    "app.services.page_memory.page_renderer": ("PageRenderResult",),
    "app.services.page_memory.page_tagger": (
        "PageTagResult",
        "tag_page_titles",
        "tag_pages",
    ),
    "app.services.page_memory.phase_a": ("build_phase_a",),
    "app.services.page_memory.scope_enrichment_service": (
        "run_single_scope_refinement",
    ),
}


@pytest.fixture(autouse=True)
def _rebind_worker_symbols() -> None:
    """Rebind worker symbols to the live modules.

    Other contract modules evict and re-import ``app.*``; without rebinding,
    this module's references (and enum identity) would point at stale copies
    while string-path monkeypatches target the live ones.
    """
    namespace = globals()
    for module_name, names in _WORKER_SYMBOLS.items():
        module = importlib.import_module(module_name)
        for name in names:
            namespace[name] = getattr(module, name)


def test_atlas_profile_activates_drawing_sheet_on_phase_a_manifest() -> None:
    profile = SimpleNamespace(
        page_count=2,
        anatomy=None,
        page_full_text_cache={1: "body", 2: "drawing"},
        page_body_text_cache={},
        routing_category=PdfRoutingCategory.ATLAS,
    )
    phase_a = build_phase_a(profile=profile, filename="atlas.pdf")
    assert phase_a.manifest["routing_category"] == "atlas"
    assert phase_a.manifest["fine_skills"] == [DRAWING_SHEET_SKILL_NAME]
    assert resolve_active_fine_skill_names(manifest=phase_a.manifest) == (
        DRAWING_SHEET_SKILL_NAME,
    )


def test_generic_document_does_not_inject_drawing_skill_prompt() -> None:
    profile = SimpleNamespace(
        page_count=1,
        anatomy=None,
        page_full_text_cache={1: "body"},
        page_body_text_cache={},
        routing_category=PdfRoutingCategory.GENERIC,
    )
    phase_a = build_phase_a(profile=profile, filename="report.pdf")
    assert phase_a.manifest["fine_skills"] == []
    prompt, *_ = build_prompt(
        "page-memory-vlm-tag",
        "",
        "",
        paras={"max_tokens": 600},
    )
    assert "is_drawing_sheet" not in prompt
    assert "drawing_title" not in prompt


def test_drawing_skill_addon_is_appended_only_when_requested() -> None:
    addon = DrawingSheetFineSkill().page_tag_prompt_addon()
    prompt, *_ = build_prompt(
        "page-memory-vlm-tag",
        "",
        "",
        paras={"max_tokens": 600, "skill_instructions": addon},
    )
    assert "is_drawing_sheet" in prompt
    assert "drawing_title" in prompt
    node_prompt, *_ = build_prompt(
        "page-memory-node-summary",
        "",
        "",
        paras={
            "max_tokens": 400,
            "node_title": "A",
            "next_title": "",
            "skill_instructions": DrawingSheetFineSkill().node_summary_prompt_addon(),
        },
    )
    assert "title block" in node_prompt
    plain_node, *_ = build_prompt(
        "page-memory-node-summary",
        "",
        "",
        paras={"max_tokens": 400, "node_title": "A", "next_title": ""},
    )
    assert "title block" not in plain_node


def test_title_prompt_appends_drawing_rule_only_when_requested() -> None:
    plain, *_ = build_prompt("page-memory-vlm-title", "", "", paras={"max_tokens": 300})
    assert "DRAWING-SHEET RULE" not in plain
    assert "Running headers, running footers, and page numbers" in plain
    prompt, *_ = build_prompt(
        "page-memory-vlm-title",
        "",
        "",
        paras={
            "max_tokens": 300,
            "skill_instructions": DrawingSheetFineSkill().title_prompt_addon(),
        },
    )
    assert "DRAWING-SHEET RULE" in prompt
    assert "<drawing_no> <drawing_title>" in prompt


def test_tag_page_titles_forwards_title_skill_addon(monkeypatch, tmp_path) -> None:
    captured: dict[str, str] = {}
    png = tmp_path / "page-1.png"
    Image.new("RGB", (8, 8), "white").save(png)

    def fake_tag_vlm_titles(page, *, model, scan_direction, skill_instructions):
        del page, model, scan_direction
        captured["skill_instructions"] = skill_instructions
        return [{"text": "A-01 Plan", "prominence": 1.0}]

    monkeypatch.setattr(
        "app.services.page_memory.page_tagger._tag_vlm_titles",
        fake_tag_vlm_titles,
    )
    results = tag_page_titles(
        pages=[
            PageRenderResult(
                page_index=1,
                image_path=str(png),
                raw_text="",
                width=8,
                height=8,
                is_landscape=False,
            )
        ],
        tag_results=[PageTagResult(page_index=1)],
        fat_leaf_pages={1},
        vlm_model="vision",
        active_skills=resolve_fine_skills((DRAWING_SHEET_SKILL_NAME,)),
    )
    assert "DRAWING-SHEET RULE" in captured["skill_instructions"]
    assert results[0].observed_titles == [{"text": "A-01 Plan", "prominence": 1.0}]


def test_mixed_pages_keep_detect_for_body_and_intact_asset_for_drawing(
    monkeypatch, tmp_path
) -> None:
    detected_pages: list[int] = []

    def fake_detect(*, page, source_name, model_name, confidence_threshold):
        del source_name, model_name, confidence_threshold
        detected_pages.append(page.page_index)
        return []

    monkeypatch.setattr(
        "app.services.page_memory.page_assets.detect_page_assets",
        fake_detect,
    )

    body_png = tmp_path / "page-1.png"
    drawing_png = tmp_path / "page-2.png"
    Image.new("RGB", (40, 50), "white").save(body_png)
    Image.new("RGB", (40, 50), "black").save(drawing_png)
    pages = [
        PageRenderResult(
            page_index=1,
            image_path=str(body_png),
            raw_text="table",
            width=20,
            height=25,
            is_landscape=False,
        ),
        PageRenderResult(
            page_index=2,
            image_path=str(drawing_png),
            raw_text="",
            width=20,
            height=25,
            is_landscape=False,
        ),
    ]
    tags_by_page = {
        1: PageTagResult(
            page_index=1,
            skill_signals={"is_drawing_sheet": False},
        ),
        2: PageTagResult(
            page_index=2,
            summary="plan",
            keywords=["A-01"],
            entities=[{"text": "A-01", "type": "identifier"}],
            skill_signals={
                "is_drawing_sheet": True,
                "drawing_title": "A-01 Plan",
            },
        ),
    }
    assets_by_page = extract_page_assets_from_renders(
        pdf_path="atlas.pdf",
        rendered_pages=pages,
        output_dir=str(tmp_path),
        model_name="vision",
        confidence_threshold=0.3,
        summary_enabled=False,
        table_merge_enabled=False,
        tags_by_page=tags_by_page,
        active_skills=resolve_fine_skills((DRAWING_SHEET_SKILL_NAME,)),
    )
    assert detected_pages == [1]
    assert 1 not in assets_by_page
    drawing_assets = assets_by_page[2]
    assert len(drawing_assets) == 1
    asset = drawing_assets[0]
    assert asset.kind == "figure"
    assert asset.bbox_px == [0, 0, 40, 50]
    assert asset.title == "A-01 Plan"
    assert asset.summary == "plan"
    assert asset.image_uri.startswith("images/")
    assert os.path.isfile(asset.image_path)


def test_drawing_sheet_pages_are_selected_even_without_has_asset() -> None:
    rendered = [
        PageRenderResult(
            page_index=1,
            image_path="/tmp/1.png",
            raw_text="",
            width=10,
            height=10,
            is_landscape=False,
        ),
        PageRenderResult(
            page_index=2,
            image_path="/tmp/2.png",
            raw_text="",
            width=10,
            height=10,
            is_landscape=False,
        ),
    ]
    page_features = [
        SimpleNamespace(page=1, has_asset=False),
        SimpleNamespace(page=2, has_asset=False),
    ]
    tags_by_page = {
        2: PageTagResult(
            page_index=2,
            skill_signals={"is_drawing_sheet": True},
        ),
    }
    selected = _select_rendered_pages_with_assets(
        rendered, page_features, tags_by_page=tags_by_page
    )
    assert [item.page_index for item in selected] == [2]


def test_config_can_enable_drawing_sheet_without_atlas_profile() -> None:
    config = PageMemoryConfig.from_mapping({"fine_skills": ["drawing_sheet"]})
    names = resolve_active_fine_skill_names(
        profile=SimpleNamespace(routing_category=PdfRoutingCategory.GENERIC),
        config=config,
    )
    assert names == (DRAWING_SHEET_SKILL_NAME,)


def test_progressive_refinement_keeps_manifest_drawing_skill(
    monkeypatch, tmp_path
) -> None:
    captured: dict[str, object] = {}

    def fake_run_single_scope(**kwargs):
        captured["fine_skills"] = kwargs["page_memory_config"].fine_skills
        return SimpleNamespace(
            hierarchy=[],
            rendered=[],
            tags=[],
            assets_by_page={},
            fine_text_by_page={},
        )

    monkeypatch.setattr(
        "app.services.page_memory.scope_enrichment_service.run_single_scope",
        fake_run_single_scope,
    )
    monkeypatch.setattr(
        "app.services.page_memory.scope_enrichment_service._assemble_scope_rows",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        "app.services.page_memory.scope_enrichment_service._build_scope_section_summaries",
        lambda **kwargs: {},
    )
    output = run_single_scope_refinement(
        scope_id="p1-2",
        pdf_path=str(tmp_path / "atlas.pdf"),
        output_dir=str(tmp_path),
        manifest={
            "filename": "atlas.pdf",
            "page_count": 2,
            "toc_pages": [],
            "next_title_by_path": {},
            "routing_category": "atlas",
            "fine_skills": [DRAWING_SHEET_SKILL_NAME],
            "scopes": [
                {
                    "scope_id": "p1-2",
                    "strategy": "fine",
                    "start_page": 1,
                    "end_page": 2,
                    "skeletons": [],
                }
            ],
        },
        page_texts={1: "body", 2: "drawing"},
        body_text_by_page={},
        page_features=[],
        page_labels=[],
        page_memory_config=PageMemoryConfig.default(),
        source_file_name="atlas.pdf",
    )
    assert captured["fine_skills"] == (DRAWING_SHEET_SKILL_NAME,)
    assert output.scope_id == "p1-2"


def test_tag_pages_forwards_drawing_skill_signals(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    png = tmp_path / "page-1.png"
    Image.new("RGB", (8, 8), "white").save(png)

    def fake_summarize(**kwargs):
        captured["prompt_paras"] = kwargs.get("prompt_paras")
        return BodySummary(
            summary="sheet",
            kind="page",
            skill_signals={
                "is_drawing_sheet": True,
                "drawing_title": "A-01 Plan",
            },
        )

    monkeypatch.setattr(
        "app.services.page_memory.page_tagger.summarize",
        fake_summarize,
    )
    results = tag_pages(
        pages=[
            PageRenderResult(
                page_index=1,
                image_path=str(png),
                raw_text="",
                width=8,
                height=8,
                is_landscape=False,
            )
        ],
        plans=[
            PagePlan(
                page_index=1,
                strategy=PageProcessingStrategy.VLM_LITE,
                reason="test",
            )
        ],
        vlm_model="vision",
        active_skills=resolve_fine_skills((DRAWING_SHEET_SKILL_NAME,)),
    )
    paras = captured["prompt_paras"]
    assert isinstance(paras, dict)
    assert "is_drawing_sheet" in str(paras.get("skill_instructions") or "")
    assert paras.get("skill_signal_keys") == [
        "is_drawing_sheet",
        "drawing_title",
    ]
    assert results[0].skill_signals == {
        "is_drawing_sheet": True,
        "drawing_title": "A-01 Plan",
    }
