"""Contract tests for the slide-structure route."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.manifest import (
    DocumentProfile,
    PageScreen,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.page_screen.config import GridLayout
from app.services.document_agent.slide_structure import route as route_module
from app.services.document_agent.slide_structure.config import SlideStructureConfig
from app.services.document_agent.slide_structure.pipeline import (
    SlideSection,
    SlideStructure,
    detect_slide_structure,
)
from app.services.document_agent.state import ProfileBlackboard

_CONFIG = SlideStructureConfig(
    sheet=GridLayout(cols=3, rows=3, thumb_width=40),
    candidates=GridLayout(cols=2, rows=2, thumb_width=80),
    vlm_concurrency=2,
    vlm_max_tokens=100,
)


class _Grids:
    """One fake image per grid; records which slides each grid shows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[int]]] = []

    def grids(self, slides: list[int], layout: GridLayout, *, name: str) -> list[str]:
        self.calls.append((name, list(slides)))
        return [
            f"{name}:{slides[i : i + layout.cells]}"
            for i in range(0, len(slides), layout.cells)
        ]


def _scripted_vlm(answers: dict[str, Any]):
    asked: list[tuple[str, list[str]]] = []

    def call(prompt: str, images: list[str], task: str) -> dict[str, Any]:
        asked.append((task, images))
        answer = answers[task]
        return cast(
            dict[str, Any], answer(prompt, images) if callable(answer) else answer
        )

    return call, asked


def test_detect_keeps_candidate_starts_and_all_or_nothing_gap_groups() -> None:
    def gap_fill(prompt: str, _images: list[str]) -> dict[str, Any]:
        if "2.1 Alpha" in prompt:
            return {
                "found": [
                    {"expected": "2.1 Alpha", "title": "2.1 Alpha", "start_slide": 12},
                    {"expected": "2.2 Beta", "title": "2.2 Beta", "start_slide": 14},
                    {
                        "expected": "2.2.1 Deep",
                        "title": "2.2.1 Deep",
                        "start_slide": 15,
                    },
                ]
            }
        return {
            "found": [
                {"expected": "1.1 Intro", "title": "1.1 Intro", "start_slide": 5},
                {"expected": "1.2 Setup", "start_slide": None},
            ]
        }

    vlm, asked = _scripted_vlm(
        {
            "structure": {
                "sections": [
                    {"title": "1 Basics", "start_slide": 3, "level": 1},
                    {"title": "2 Methods", "start_slide": 11, "level": 1},
                    {"title": "3 Results", "start_slide": 16, "level": 1},
                    {"title": "Listed only", "start_slide": 7, "level": 1},
                ],
                "ignored_slides": [],
                "gaps": [
                    {
                        "expected": [
                            {"title": "1.1 Intro", "level": 2},
                            {"title": "1.2 Setup", "level": 2},
                        ],
                        "after_slide": 3,
                        "before_slide": 11,
                    },
                    {
                        "expected": [{"title": "2.1 Alpha", "level": 2}],
                        "after_slide": 11,
                        "before_slide": 16,
                    },
                    {
                        "expected": [
                            {"title": "2.2 Beta", "level": 2},
                            {"title": "2.2.1 Deep", "level": 3},
                        ],
                        "after_slide": 11,
                        "before_slide": 16,
                    },
                ],
            },
            "gap_fill": gap_fill,
        }
    )
    grids = _Grids()

    structure = detect_slide_structure(
        page_count=20, candidates=[3, 11, 16], grids=grids, config=_CONFIG, vlm=vlm
    )

    assert structure.candidates == [3, 11, 16]
    assert ("candidates", [3, 11, 16]) in grids.calls
    assert [
        (s.title, s.start_slide, s.level, s.filled) for s in structure.sections
    ] == [
        ("1 Basics", 3, 1, False),
        ("2 Methods", 11, 1, False),
        ("2.1 Alpha", 12, 2, True),
        ("2.2 Beta", 14, 2, True),
        ("2.2.1 Deep", 15, 3, True),
        ("3 Results", 16, 1, False),
    ]
    # One group per slide range, whatever the levels, so they are located together.
    groups = {(g["first"], g["last"]): g for g in structure.gap_groups}
    assert groups[(4, 10)]["accepted"] is False
    assert groups[(12, 15)]["accepted"] is True
    assert groups[(12, 15)]["expected"] == {
        "2.1 Alpha": 2,
        "2.2 Beta": 2,
        "2.2.1 Deep": 3,
    }
    assert [task for task, _ in asked] == ["structure", "gap_fill", "gap_fill"]
    assert ("gap_12-15", list(range(12, 16))) in grids.calls


def test_detect_without_candidates_makes_no_vlm_call() -> None:
    vlm, asked = _scripted_vlm({})
    grids = _Grids()

    structure = detect_slide_structure(
        page_count=12, candidates=[], grids=grids, config=_CONFIG, vlm=vlm
    )

    assert structure.sections == []
    assert asked == []
    assert grids.calls == []


def test_toc_entries_keep_one_row_per_title_path() -> None:
    entries = route_module.toc_entries(
        [
            SlideSection("Part A", 1, 2),
            SlideSection("Summary", 2, 4),
            SlideSection("Part B", 1, 6),
            SlideSection("Summary", 2, 8),
            SlideSection("Summary", 2, 9),
            SlideSection("B", 2, 10),
        ]
    )

    assert [(e["heading"], e["level"], e["physical_page"]) for e in entries] == [
        ("Part A", 1, 2),
        ("Summary", 2, 4),
        ("Part B", 1, 6),
        ("Summary", 2, 8),
    ]


def _route_ctx(
    tmp_path: Path, page_count: int, screen: PageScreen | None = None
) -> ToolContext:
    blackboard = ProfileBlackboard(
        page_count=page_count, page_screen=screen or PageScreen()
    )
    return ToolContext(
        pdf_path=str(tmp_path / "deck.pdf"),
        job_id="job-deck",
        blackboard=blackboard,
        trace=None,
        output_dir=str(tmp_path),
    )


def _patch_detection(
    monkeypatch: pytest.MonkeyPatch, structure: SlideStructure
) -> list[dict[str, Any]]:
    """Stub detection; return the recorded detection kwargs."""
    detections: list[dict[str, Any]] = []

    def detect(**kwargs: Any) -> SlideStructure:
        detections.append(kwargs)
        return structure

    monkeypatch.setattr(route_module, "ContactSheets", lambda ctx, **kwargs: None)
    monkeypatch.setattr(route_module, "vision_call", lambda ctx, **kwargs: None)
    monkeypatch.setattr(route_module, "detect_slide_structure", detect)
    return detections


def test_route_anchors_sections_into_skeleton_ranges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.page_memory.skeleton_extractor import extract_section_skeletons

    detections = _patch_detection(
        monkeypatch,
        SlideStructure(
            sections=[
                SlideSection("Deck", 1, 2),
                SlideSection("1 Basics", 2, 3),
                SlideSection("2 Methods", 2, 6),
                SlideSection("2.1 Alpha", 3, 7, filled=True),
            ],
            candidates=[2, 3, 6],
        ),
    )
    # A slide that is both a TOC and a divider (3) is one candidate.
    ctx = _route_ctx(
        tmp_path,
        page_count=10,
        screen=PageScreen(toc_pages=[2, 3], divider_pages=[3, 6]),
    )

    result = route_module.run_slide_structure_route(ctx)

    assert [d["candidates"] for d in detections] == [[2, 3, 6]]
    assert result.status == "ok"
    assert ctx.blackboard.toc_result.method == "slide_structure"
    assert ctx.blackboard.toc_result.toc_pages == []
    anatomy = SimpleNamespace(
        page_count=10,
        toc_hierarchies=ctx.blackboard.toc_hierarchies,
        skeleton_anchor=ctx.blackboard.skeleton_anchor,
        skeleton_nodes=ctx.blackboard.skeleton_nodes,
        pending_skeleton_anchors=ctx.blackboard.pending_skeleton_anchors,
        toc_result=ctx.blackboard.toc_result,
    )
    skeletons = extract_section_skeletons(
        anatomy=anatomy, filename="deck.pdf", page_texts={}
    )
    ranges = {s.title: (s.start_page, s.end_page, s.level) for s in skeletons}
    # A section ends on the page where the next one starts (the anchoring's shared-boundary rule).
    assert ranges["1 Basics"] == (3, 6, 2)
    # A parent with children keeps only its own pages, up to its first child.
    assert ranges["2 Methods"] == (6, 7, 2)
    assert ranges["2.1 Alpha"] == (7, 10, 3)


def test_route_without_sections_leaves_the_deck_flat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_detection(monkeypatch, SlideStructure(sections=[], candidates=[]))
    ctx = _route_ctx(tmp_path, page_count=6)

    route_module.run_slide_structure_route(ctx)

    assert ctx.blackboard.toc_result.method == "none"
    assert ctx.blackboard.toc_hierarchies is None
    assert ctx.blackboard.skeleton_nodes is None


def test_route_degrades_like_toc_extraction_when_detection_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_detection(**_kwargs: Any) -> SlideStructure:
        raise TimeoutError("vlm timed out")

    _patch_detection(monkeypatch, SlideStructure(sections=[], candidates=[]))
    monkeypatch.setattr(route_module, "detect_slide_structure", failing_detection)
    ctx = _route_ctx(tmp_path, page_count=6)

    result = route_module.run_slide_structure_route(ctx)

    assert result.status == "ok"
    assert result.warnings
    assert ctx.blackboard.toc_result.method == "none"
    assert ctx.blackboard.toc_result.failure_kind == "degraded"
    assert ctx.blackboard.toc_hierarchies is None
    assert ctx.blackboard.skeleton_nodes is None


def test_route_without_page_screen_degrades_without_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    detections = _patch_detection(
        monkeypatch, SlideStructure(sections=[], candidates=[])
    )
    ctx = _route_ctx(tmp_path, page_count=6)
    ctx.blackboard.page_screen = None

    result = route_module.run_slide_structure_route(ctx)

    assert detections == []
    assert result.warnings
    assert ctx.blackboard.toc_result.method == "none"
    assert ctx.blackboard.toc_result.failure_kind == "degraded"
    assert ctx.blackboard.toc_hierarchies is None


@pytest.mark.parametrize(
    ("routing", "expected_route"),
    [("slides", "slides"), ("generic", "toc"), ("atlas", "toc"), (None, "toc")],
)
def test_coordinator_routes_slide_decks_to_slide_structure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routing: str | None,
    expected_route: str,
) -> None:
    from app.services.document_agent import coordinator as coordinator_module

    called: list[str] = []
    monkeypatch.setattr(
        coordinator_module,
        "run_page_screen",
        lambda ctx: called.append("screen") or ToolResult(status="ok"),
    )
    monkeypatch.setattr(
        coordinator_module,
        "run_slide_structure_route",
        lambda ctx: called.append("slides") or ToolResult(status="ok"),
    )
    monkeypatch.setattr(
        coordinator_module.ProfileCoordinator,
        "_run_toc_extraction_pipeline",
        lambda self: called.append("toc"),
    )
    coordinator = coordinator_module.ProfileCoordinator(
        pdf_path=str(tmp_path / "doc.pdf"),
        job_id="job-route",
        output_dir=str(tmp_path),
        db=None,
    )
    # The route is chosen from the profile already on the blackboard; it must
    # not classify the document again.
    monkeypatch.setattr(
        coordinator_module.ProfileCoordinator,
        "_ensure_coarse_profile",
        lambda self, actor: pytest.fail("coarse profile must not run again"),
    )
    if routing is not None:
        coordinator.blackboard.document_profile = DocumentProfile(
            is_scanned=False,
            category=["OTHER"],
            category_label="Doc",
            routing_category=routing,
        )

    coordinator._ensure_toc_profile()  # noqa: SLF001

    # Every format is screened before its route.
    assert called == ["screen", expected_route]
