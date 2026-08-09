"""Contract tests for TOC Phase-1 batched VLM anchor confirmation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.document_agent.budget import BudgetTracker, StageEnvelope
from app.services.document_agent.manifest import TocAnchorPage
from app.services.document_agent.tools import extract_toc_with_boundaries as toc_tool


def _anchors(tmp_path: Path, pages: list[int]) -> list[TocAnchorPage]:
    out: list[TocAnchorPage] = []
    for page in pages:
        png = tmp_path / f"toc_anchor_page_{page}.png"
        png.write_bytes(b"fake-png")
        out.append(
            TocAnchorPage(
                page=page,
                png_path=str(png),
                source="text_scan",
            )
        )
    return out


def test_iter_chunks_uses_boundary_step_size() -> None:
    items = list(range(12))
    chunks = toc_tool._iter_chunks(items, toc_tool.BOUNDARY_STEP_PAGES)  # noqa: SLF001
    assert chunks == [
        [0, 1, 2, 3, 4],
        [5, 6, 7, 8, 9],
        [10, 11],
    ]


def test_vlm_confirm_anchors_batches_and_merges_partial_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One confirm chunk may fail; successful chunks still contribute confirmed pages."""
    anchors = _anchors(tmp_path, [5, 11, 40, 44, 55, 78, 97])
    assert toc_tool.BOUNDARY_STEP_PAGES == 5
    # 7 anchors → 2 chunks: [5..55] and [78, 97]

    call_pages: list[list[int]] = []

    class _FakeClient:
        def chat_completion_with_usage(self, **kwargs: Any) -> tuple[str, dict[str, int]]:
            content = kwargs["messages"][0]["content"]
            pages = []
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = str(part.get("text") or "")
                if text.startswith("\n--- Page ") and text.endswith(" ---"):
                    pages.append(int(text[len("\n--- Page ") : -len(" ---")]))
            call_pages.append(pages)
            if 78 in pages:
                raise RuntimeError("simulated truncated JSON")
            payload = {
                "pages": [
                    {
                        "page": page,
                        "is_toc_start": page in {5, 11, 40},
                        "reason": "ok",
                    }
                    for page in pages
                ]
            }
            return json.dumps(payload), {"total_tokens": 100}

    monkeypatch.setattr(
        "shared.services.ai.llm_overrides.get_vision_client",
        lambda requested_model=None: (_FakeClient(), requested_model or "fake-vlm"),
    )

    budget = BudgetTracker(
        plan_budget=50_000,
        visual_budget=200_000,
        visual_stage_envelopes={
            "toc_confirm": StageEnvelope(min_guarantee=0, cap=None),
        },
    )
    confirmed, confirm_failed, evidence = toc_tool._vlm_confirm_anchors(  # noqa: SLF001
        anchors,
        model="fake-vlm",
        budget=budget,
    )

    assert sorted(call_pages[0] + call_pages[1]) == [5, 11, 40, 44, 55, 78, 97]
    assert {tuple(pages) for pages in call_pages} == {
        (5, 11, 40, 44, 55),
        (78, 97),
    }
    assert confirm_failed is False
    assert [a.page for a in confirmed] == [5, 11, 40]
    assert {e.page_index for e in evidence} == {5, 11, 40, 44, 55, 78, 97}


def test_vlm_confirm_anchors_all_chunks_fail_sets_confirm_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchors = _anchors(tmp_path, [5, 11])

    class _FakeClient:
        def chat_completion_with_usage(self, **kwargs: Any) -> tuple[str, dict[str, int]]:
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "shared.services.ai.llm_overrides.get_vision_client",
        lambda requested_model=None: (_FakeClient(), requested_model or "fake-vlm"),
    )

    confirmed, confirm_failed, _evidence = toc_tool._vlm_confirm_anchors(  # noqa: SLF001
        anchors,
        model="fake-vlm",
        budget=None,
    )
    assert confirmed == []
    assert confirm_failed is True
