"""OUTLINE route inside run_toc_anchoring: skip calibrate VLM when outline wins."""

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

from app.services.document_agent.manifest import TocResult, ToolContext, ToolResult
from app.services.document_agent.state import ProfileBlackboard
from app.services.document_agent.structure.toc_anchoring import run_toc_anchoring


def _outline_roots() -> list[dict[str, Any]]:
    return [
        {
            "title": "第一章",
            "level": 1,
            "page": 10,
            "children": [
                {
                    "title": "第二章",
                    "level": 2,
                    "page": 15,
                    "children": [],
                }
            ],
        }
    ]


def _ctx(*, toc_pages: list[int] | None = None) -> ToolContext:
    pages = [2, 3, 4, 5] if toc_pages is None else toc_pages
    blackboard = ProfileBlackboard(page_count=20)
    blackboard.page_full_text_cache = {
        2: "目录\n第一章 ...... 3\n第二章 ...... 8",
        3: "目录续\n更多条目",
        4: "目录续\n再多条目",
        5: "目录尾",
        10: "第一章\n正文",
        15: "第二章\n正文",
    }
    # Written by find-stage probe.outline; anchoring only consumes it.
    blackboard.pdf_outline_roots = _outline_roots()
    blackboard.toc_result = TocResult(
        method="vlm_batch",
        toc_pages=pages,
        notes="confirmed toc pages from extract",
    )
    blackboard.toc_hierarchies = [
        {
            "source": "vlm",
            "toc_range": [2, 5],
            "toc_range_unit": "page",
            "toc_with_level": [
                {"heading": "第一章", "level": 1, "page_number": 3},
                {"heading": "第二章", "level": 1, "page_number": 8},
            ],
        }
    ]
    return ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="job-outline-anchor",
        blackboard=blackboard,
        trace=None,
        settings={},
    )


def _no_null_parent_locate(**kwargs: Any) -> tuple[dict[Any, Any], list[Any]]:
    return dict(kwargs["match_overrides"]), []


def test_outline_wins_skips_calibrate_and_keeps_confirmed_toc_pages() -> None:
    ctx = _ctx()
    calibrate_calls = {"count": 0}
    judge_calls: list[dict[str, Any]] = []
    probe_calls = {"count": 0}

    def fake_judge(tool_ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        del tool_ctx
        judge_calls.append(args)
        return ToolResult(
            status="ok",
            payload={"choice": "outline", "reason": "broader coverage"},
        )

    def fake_probe(tool_ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        del tool_ctx, args
        probe_calls["count"] += 1
        return ToolResult(status="ok", payload={"roots": []})

    def fake_calibrate(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        calibrate_calls["count"] += 1
        raise AssertionError("calibrate_offset must not run when outline wins")

    with (
        patch(
            "app.services.document_agent.tools.probe_outline.probe_outline",
            side_effect=fake_probe,
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
            "locate_null_page_node_overrides",
            side_effect=_no_null_parent_locate,
        ),
    ):
        run_toc_anchoring(ctx)

    assert probe_calls["count"] == 0
    assert calibrate_calls["count"] == 0
    assert len(judge_calls) == 1
    assert judge_calls[0]["toc_pages"] == [2, 3, 4, 5]
    assert ctx.blackboard.toc_result is not None
    assert ctx.blackboard.toc_result.toc_pages == [2, 3, 4, 5]
    assert ctx.blackboard.toc_result.method == "pdf_outline"
    assert ctx.blackboard.skeleton_anchor is not None
    assert ctx.blackboard.skeleton_anchor["source"] == "pdf_outline"
    assert ctx.blackboard.skeleton_anchor["offset_status"] == "ok"
    assert ctx.blackboard.toc_hierarchies is not None
    assert ctx.blackboard.toc_hierarchies[0]["source"] == "pdf_outline"


def test_outline_without_toc_pages_adopts_without_judge() -> None:
    ctx = _ctx(toc_pages=[])
    judge_calls = {"count": 0}

    def fake_judge(tool_ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        del tool_ctx, args
        judge_calls["count"] += 1
        return ToolResult(status="ok", payload={"choice": "outline"})

    with (
        patch(
            "app.services.document_agent.tools.judge_toc_source.judge_toc_source",
            side_effect=fake_judge,
        ),
        patch(
            "app.services.document_agent.structure.anchoring_primitives."
            "locate_null_page_node_overrides",
            side_effect=_no_null_parent_locate,
        ),
    ):
        run_toc_anchoring(ctx)

    assert judge_calls["count"] == 0
    assert ctx.blackboard.skeleton_anchor is not None
    assert ctx.blackboard.skeleton_anchor["source"] == "pdf_outline"


def test_printed_toc_wins_uses_vlm_calibrate_path() -> None:
    ctx = _ctx()
    vlm_anchor_calls = {"count": 0}

    def fake_judge(tool_ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        del tool_ctx, args
        return ToolResult(
            status="ok",
            payload={"choice": "printed_toc", "reason": "printed finer"},
        )

    def fake_anchor_hierarchy(**kwargs: Any) -> Any:
        del kwargs
        vlm_anchor_calls["count"] += 1
        from app.services.document_agent.structure.anchoring_primitives import (
            SkeletonAnchor,
        )

        return [], SkeletonAnchor(
            offset=0,
            offset_status="ok",
            match_overrides={},
            null_page_report=[],
            bulk_count=0,
        )

    with (
        patch(
            "app.services.document_agent.tools.judge_toc_source.judge_toc_source",
            side_effect=fake_judge,
        ),
        patch(
            "app.services.document_agent.calibration.orchestrator.anchor_hierarchy",
            side_effect=fake_anchor_hierarchy,
        ),
    ):
        run_toc_anchoring(ctx)

    assert vlm_anchor_calls["count"] == 1
    assert ctx.blackboard.toc_result is not None
    assert ctx.blackboard.toc_result.method == "vlm_batch"
    assert ctx.blackboard.toc_hierarchies is not None
    assert ctx.blackboard.toc_hierarchies[0]["source"] == "vlm"


def test_merge_printed_toc_texts_drops_lines_shared_across_pages() -> None:
    from app.services.document_agent.tools.judge_toc_source import (
        merge_printed_toc_texts,
    )

    merged = merge_printed_toc_texts(
        [
            "Manual Title\nCONTENTS\nChapter 1 ...... 10",
            "Manual Title\nCONTENTS\nChapter 2 ...... 20",
            "Manual Title\nChapter 3 ...... 30",
        ]
    )
    assert merged == "Chapter 1 ...... 10\nChapter 2 ...... 20\nChapter 3 ...... 30"


def test_merge_printed_toc_texts_single_page_unchanged() -> None:
    from app.services.document_agent.tools.judge_toc_source import (
        merge_printed_toc_texts,
    )

    text = "CONTENTS\nChapter 1 ...... 10\nChapter 1 ...... 10"
    assert merge_printed_toc_texts([text]) == text
