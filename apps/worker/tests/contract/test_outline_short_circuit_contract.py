"""OUTLINE short-circuit: skip VLM extract/calibrate when bookmarks win the compare."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.coordinator import ProfileCoordinator
from app.services.document_agent.manifest import (
    TocAnchorPage,
    TocResult,
    ToolContext,
    ToolResult,
)


def _coordinator(*, page_count: int = 20) -> ProfileCoordinator:
    coordinator = ProfileCoordinator(
        pdf_path="/tmp/doc.pdf",
        job_id="job-outline",
    )
    coordinator.blackboard.page_count = page_count
    coordinator.blackboard.page_full_text_cache = {
        1: "Contents\nChapter One ...... 3",
        3: "Chapter One\nBody starts here",
        8: "Chapter Two\nMore body",
    }
    coordinator.blackboard.toc_anchor_pages = [
        TocAnchorPage(page=1, png_path="/tmp/toc-1.png"),
    ]
    return coordinator


def _outline_payload() -> dict[str, Any]:
    return {
        "source": "pdf_outline",
        "roots": [
            {
                "title": "Chapter One",
                "level": 1,
                "page": 3,
                "children": [
                    {
                        "title": "Chapter Two",
                        "level": 2,
                        "page": 8,
                        "children": [],
                    }
                ],
            }
        ],
    }


def _fake_outline(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del ctx, args
    return ToolResult(status="ok", payload=_outline_payload())


def _no_null_parent_locate(**kwargs: Any) -> tuple[dict[Any, Any], list[Any]]:
    return dict(kwargs["match_overrides"]), []


def test_outline_adopt_skips_extract_and_calibrate_with_one_judge() -> None:
    coordinator = _coordinator()
    extract_calls = {"count": 0}
    calibrate_calls = {"count": 0}
    judge_calls: list[dict[str, Any]] = []

    def fake_find(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        del ctx, args
        return ToolResult(status="ok", payload={"pages": [1]})

    def fake_extract(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        del ctx, args
        extract_calls["count"] += 1
        return ToolResult(status="ok", payload={})

    def fake_judge(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        del ctx
        judge_calls.append(args)
        return ToolResult(
            status="ok",
            payload={"choice": "outline", "reason": "broader coverage"},
        )

    def fake_calibrate(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        calibrate_calls["count"] += 1
        raise AssertionError("calibrate_offset must not run on outline adopt")

    with (
        patch(
            "app.services.document_agent.coordinator.REGISTRY.dispatch",
            side_effect=lambda name, ctx, args: (
                fake_find(ctx, args)
                if name == "find.toc_anchor_pages"
                else fake_extract(ctx, args)
            ),
        ),
        patch(
            "app.services.document_agent.tools.probe_outline.probe_outline",
            side_effect=_fake_outline,
        ),
        patch(
            "app.services.document_agent.tools.judge_toc_source.judge_toc_source",
            side_effect=fake_judge,
        ),
        patch(
            "app.services.document_agent.calibration.service.calibrate_offset",
            side_effect=fake_calibrate,
        ),
        patch(
            "app.services.document_agent.structure.anchoring_primitives."
            "locate_null_page_parent_overrides",
            side_effect=_no_null_parent_locate,
        ),
    ):
        coordinator._run_toc_extraction_pipeline()

    assert extract_calls["count"] == 0
    assert calibrate_calls["count"] == 0
    assert len(judge_calls) == 1
    # Every printed TOC page is compared; no page cap.
    assert judge_calls[0]["toc_pages"] == [1]
    assert "Chapter One" in judge_calls[0]["outline_digest"]
    assert coordinator.blackboard.toc_result is not None
    assert coordinator.blackboard.toc_result.method == "pdf_outline"
    assert coordinator.blackboard.skeleton_anchor is not None
    assert coordinator.blackboard.skeleton_anchor["source"] == "pdf_outline"
    assert coordinator.blackboard.skeleton_anchor["offset_status"] == "ok"


def test_outline_without_toc_pages_adopts_without_judge() -> None:
    coordinator = _coordinator()
    judge_calls = {"count": 0}

    def fake_find(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        del args
        ctx.blackboard.toc_anchor_pages = []
        return ToolResult(status="ok", payload={"pages": []})

    def fake_judge(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        del ctx, args
        judge_calls["count"] += 1
        return ToolResult(status="ok", payload={"choice": "outline"})

    with (
        patch(
            "app.services.document_agent.coordinator.REGISTRY.dispatch",
            side_effect=lambda name, ctx, args: fake_find(ctx, args),
        ),
        patch(
            "app.services.document_agent.tools.probe_outline.probe_outline",
            side_effect=_fake_outline,
        ),
        patch(
            "app.services.document_agent.tools.judge_toc_source.judge_toc_source",
            side_effect=fake_judge,
        ),
        patch(
            "app.services.document_agent.structure.anchoring_primitives."
            "locate_null_page_parent_overrides",
            side_effect=_no_null_parent_locate,
        ),
    ):
        coordinator._run_toc_extraction_pipeline()

    assert judge_calls["count"] == 0
    assert coordinator.blackboard.toc_result is not None
    assert coordinator.blackboard.toc_result.method == "pdf_outline"


def test_printed_toc_wins_falls_back_to_extract() -> None:
    coordinator = _coordinator()
    extract_calls = {"count": 0}

    def fake_find(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        del ctx, args
        return ToolResult(status="ok", payload={"pages": [1]})

    def fake_extract(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        del args
        extract_calls["count"] += 1
        ctx.blackboard.toc_hierarchies = [
            {
                "source": "vlm",
                "toc_with_level": [
                    {"heading": "Chapter One", "level": 1, "page_number": 1},
                ],
            }
        ]
        ctx.blackboard.toc_result = TocResult(
            method="vlm_batch",
            toc_pages=[1],
            notes="vlm fallback",
        )
        return ToolResult(status="ok", payload={})

    def fake_judge(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        del ctx, args
        return ToolResult(
            status="ok",
            payload={"choice": "printed_toc", "reason": "printed toc is finer"},
        )

    with (
        patch(
            "app.services.document_agent.coordinator.REGISTRY.dispatch",
            side_effect=lambda name, ctx, args: (
                fake_find(ctx, args)
                if name == "find.toc_anchor_pages"
                else fake_extract(ctx, args)
            ),
        ),
        patch(
            "app.services.document_agent.tools.probe_outline.probe_outline",
            side_effect=_fake_outline,
        ),
        patch(
            "app.services.document_agent.tools.judge_toc_source.judge_toc_source",
            side_effect=fake_judge,
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring.run_toc_anchoring",
            side_effect=lambda ctx: None,
        ),
    ):
        coordinator._run_toc_extraction_pipeline()

    assert extract_calls["count"] == 1
    assert coordinator.blackboard.toc_result is not None
    assert coordinator.blackboard.toc_result.method == "vlm_batch"
