"""Contract tests for TOC Phase-2 concurrent VLM with serial batch renders."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import gevent
import pytest

from app.services.document_agent.manifest import TocAnchorPage
from app.services.document_agent.tools import extract_toc_with_boundaries as toc_tool
from app.services.document_agent.tools.vlm_toc_extractor import (
    BatchPageResult,
    BatchTocResult,
)


def _anchors(tmp_path: Path, pages: list[int]) -> list[TocAnchorPage]:
    out: list[TocAnchorPage] = []
    for page in pages:
        png = tmp_path / f"toc_anchor_page_{page}.png"
        png.write_bytes(b"anchor-png")
        out.append(
            TocAnchorPage(page=page, png_path=str(png), source="vlm")
        )
    return out


def _anchor_from_png(png_path: str) -> int:
    name = Path(png_path).name
    if name.startswith("toc_anchor_page_"):
        return int(name[len("toc_anchor_page_") : -len(".png")])
    # toc_a{anchor}_p{page}.png
    stem = name.removesuffix(".png")
    anchor_part, _sep, _page_part = stem.partition("_p")
    assert anchor_part.startswith("toc_a"), png_path
    return int(anchor_part[len("toc_a") :])


def _batch_result(
    *,
    toc_pages: list[int],
    non_toc_pages: list[int],
    entries: list[dict[str, Any]] | None = None,
    page_results: list[BatchPageResult] | None = None,
) -> BatchTocResult:
    entry_list = list(entries or [])
    if page_results is None:
        # Callers pass ordered partitions (TOC prefix then non-TOC suffix).
        page_results = []
        for index, page in enumerate(toc_pages):
            page_entries = entry_list if index == 0 else []
            page_results.append(
                BatchPageResult(page=page, is_toc=True, entries=page_entries)
            )
        page_results.extend(
            BatchPageResult(page=page, is_toc=False, entries=[])
            for page in non_toc_pages
        )
    all_entries = list(entry_list)
    if not all_entries:
        for page_result in page_results:
            all_entries.extend(list(page_result.entries or []))
    return BatchTocResult(
        page_results=page_results,
        toc_pages=list(toc_pages),
        non_toc_pages=list(non_toc_pages),
        all_entries=all_entries,
        meta={"ok": True},
    )


def _fake_batch_render(worker_fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Match ``_render_expand_window_worker`` args: pages list in one spawn."""
    pages = list(args[1])
    output_dir = Path(args[2])
    anchor_page = int(args[4])
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for page_num in pages:
        png_path = output_dir / f"toc_a{anchor_page}_p{page_num}.png"
        png_path.write_bytes(b"png")
        results.append({"page": page_num, "png_path": str(png_path)})
    return {"ok": True, "results": results}


def test_extract_regions_runs_per_anchor_and_merges_in_page_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    confirmed = _anchors(tmp_path, [10, 40, 80])
    calls: list[int] = []
    render_calls: list[list[int]] = []

    def _tracking_render(worker_fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        render_calls.append(list(args[1]))
        return _fake_batch_render(worker_fn, *args, **kwargs)

    def _fake_batch(
        *,
        page_pngs: list[tuple[int, str]],
        model: str,
        previous_entries: list[dict[str, Any]] | None = None,
    ) -> BatchTocResult:
        first_page = page_pngs[0][0]
        anchor = _anchor_from_png(page_pngs[0][1])
        calls.append(anchor)
        assert previous_entries in (None, [])
        return _batch_result(
            toc_pages=[first_page],
            non_toc_pages=[p for p, _ in page_pngs[1:]],
            entries=[
                {
                    "title": f"Section {anchor}",
                    "page": first_page,
                    "level": 1,
                }
            ],
        )

    monkeypatch.setattr(toc_tool, "run_in_child_process", _tracking_render)
    monkeypatch.setattr(
        "app.services.document_agent.tools.vlm_toc_extractor.vlm_extract_toc_batch",
        _fake_batch,
    )
    monkeypatch.setattr(
        toc_tool,
        "vlm_entries_to_toc_hierarchies",
        lambda entries, **kwargs: [
            {
                "toc_range": [entries[0]["page"], entries[0]["page"]],
                "toc_tree": {entries[0]["title"]: {}},
            }
        ],
    )

    regions = toc_tool._extract_regions_for_confirmed_anchors(  # noqa: SLF001
        confirmed,
        pdf_path="/tmp/doc.pdf",
        page_count=100,
        output_dir=str(tmp_path / "toc_pages"),
        dpi=72,
        model="fake-vlm",
    )

    assert [r.anchor_page for r in regions] == [10, 40, 80]
    assert all(r.error is None for r in regions)
    assert sorted(calls) == [10, 40, 80]
    # One spawn per window; start page reused from Phase1 anchor PNG.
    assert sorted(render_calls) == [
        [11, 12, 13, 14],
        [41, 42, 43, 44],
        [81, 82, 83, 84],
    ]


def test_extract_regions_keeps_success_when_one_anchor_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    confirmed = _anchors(tmp_path, [10, 40])

    def _fake_batch(
        *,
        page_pngs: list[tuple[int, str]],
        model: str,
        previous_entries: list[dict[str, Any]] | None = None,
    ) -> BatchTocResult:
        anchor = _anchor_from_png(page_pngs[0][1])
        if anchor == 40:
            raise RuntimeError("simulated region VLM failure")
        first_page = page_pngs[0][0]
        return _batch_result(
            toc_pages=[first_page],
            non_toc_pages=[p for p, _ in page_pngs[1:]],
            entries=[{"title": "Intro", "page": first_page, "level": 1}],
        )

    monkeypatch.setattr(toc_tool, "run_in_child_process", _fake_batch_render)
    monkeypatch.setattr(
        "app.services.document_agent.tools.vlm_toc_extractor.vlm_extract_toc_batch",
        _fake_batch,
    )
    monkeypatch.setattr(
        toc_tool,
        "vlm_entries_to_toc_hierarchies",
        lambda entries, **kwargs: [
            {
                "toc_range": [entries[0]["page"], entries[0]["page"]],
                "toc_tree": {entries[0]["title"]: {}},
            }
        ],
    )

    regions = toc_tool._extract_regions_for_confirmed_anchors(  # noqa: SLF001
        confirmed,
        pdf_path="/tmp/doc.pdf",
        page_count=100,
        output_dir=str(tmp_path / "toc_pages"),
        dpi=72,
        model="fake-vlm",
    )

    assert regions[0].anchor_page == 10
    assert regions[0].error is None
    assert regions[0].entries
    assert regions[1].anchor_page == 40
    assert regions[1].error is not None
    assert "simulated region VLM failure" in regions[1].error


def test_phase2_serial_batch_render_with_concurrent_vlm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch renders never overlap; VLM calls may overlap across anchors."""
    confirmed = _anchors(tmp_path, [10, 40, 80])
    render_active = 0
    render_max = 0
    vlm_active = 0
    vlm_max = 0
    spawn_count = 0

    def _fake_render(worker_fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal render_active, render_max, spawn_count
        spawn_count += 1
        render_active += 1
        render_max = max(render_max, render_active)
        gevent.sleep(0.02)
        result = _fake_batch_render(worker_fn, *args, **kwargs)
        render_active -= 1
        return result

    def _fake_batch(
        *,
        page_pngs: list[tuple[int, str]],
        model: str,
        previous_entries: list[dict[str, Any]] | None = None,
    ) -> BatchTocResult:
        nonlocal vlm_active, vlm_max
        vlm_active += 1
        vlm_max = max(vlm_max, vlm_active)
        gevent.sleep(0.2)
        first_page = page_pngs[0][0]
        anchor = _anchor_from_png(page_pngs[0][1])
        vlm_active -= 1
        return _batch_result(
            toc_pages=[first_page],
            non_toc_pages=[p for p, _ in page_pngs[1:]],
            entries=[{"title": f"S{anchor}", "page": first_page, "level": 1}],
        )

    monkeypatch.setattr(toc_tool, "run_in_child_process", _fake_render)
    monkeypatch.setattr(
        "app.services.document_agent.tools.vlm_toc_extractor.vlm_extract_toc_batch",
        _fake_batch,
    )
    monkeypatch.setattr(
        toc_tool,
        "vlm_entries_to_toc_hierarchies",
        lambda entries, **kwargs: [
            {
                "toc_range": [entries[0]["page"], entries[0]["page"]],
                "toc_tree": {entries[0]["title"]: {}},
            }
        ],
    )

    regions = toc_tool._extract_regions_for_confirmed_anchors(  # noqa: SLF001
        confirmed,
        pdf_path="/tmp/doc.pdf",
        page_count=100,
        output_dir=str(tmp_path / "toc_pages"),
        dpi=72,
        model="fake-vlm",
    )

    assert all(r.error is None for r in regions)
    assert render_max == 1
    assert spawn_count == 3  # one batch spawn per anchor window
    assert vlm_max >= 2


def test_contiguous_toc_prefix_stops_at_first_non_toc() -> None:
    page_results = [
        BatchPageResult(
            page=40,
            is_toc=True,
            entries=[{"title": "3.1", "page_number": 1, "level": 1}],
        ),
        BatchPageResult(page=41, is_toc=False, entries=[]),
        BatchPageResult(page=42, is_toc=False, entries=[]),
        BatchPageResult(page=43, is_toc=False, entries=[]),
        BatchPageResult(
            page=44,
            is_toc=True,
            entries=[{"title": "4.1", "page_number": 1, "level": 1}],
        ),
    ]
    kept_pages, kept_entries = toc_tool._contiguous_toc_prefix(page_results)  # noqa: SLF001
    assert kept_pages == [40]
    assert kept_entries == [{"title": "3.1", "page_number": 1, "level": 1}]


def test_should_expand_requires_empty_non_toc_and_full_window_end() -> None:
    expand = toc_tool._should_expand_toc_window  # noqa: SLF001
    assert expand(
        batch_start=40,
        non_toc_pages=[],
        kept_toc_pages=[40, 41, 42, 43, 44],
    )
    # Mid-window break: last TOC may still equal full-window end.
    assert not expand(
        batch_start=40,
        non_toc_pages=[41, 42, 43],
        kept_toc_pages=[40],
    )
    # Short end-of-doc window: all TOC but not a full step.
    assert not expand(
        batch_start=98,
        non_toc_pages=[],
        kept_toc_pages=[98, 99, 100],
    )


def test_extract_region_drops_post_break_toc_and_does_not_expand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ch3-style: TOC then body then next-chapter TOC in one 5-page window."""
    confirmed = _anchors(tmp_path, [40])
    vlm_rounds: list[list[int]] = []

    def _fake_batch(
        *,
        page_pngs: list[tuple[int, str]],
        model: str,
        previous_entries: list[dict[str, Any]] | None = None,
    ) -> BatchTocResult:
        pages = [page for page, _ in page_pngs]
        vlm_rounds.append(pages)
        assert pages == [40, 41, 42, 43, 44]
        return _batch_result(
            toc_pages=[40, 44],
            non_toc_pages=[41, 42, 43],
            page_results=[
                BatchPageResult(
                    page=40,
                    is_toc=True,
                    entries=[
                        {"title": "3.1 INTRODUCTION", "page_number": 1, "level": 1}
                    ],
                ),
                BatchPageResult(page=41, is_toc=False, entries=[]),
                BatchPageResult(page=42, is_toc=False, entries=[]),
                BatchPageResult(page=43, is_toc=False, entries=[]),
                BatchPageResult(
                    page=44,
                    is_toc=True,
                    entries=[
                        {"title": "4.1 INTRODUCTION", "page_number": 1, "level": 1}
                    ],
                ),
            ],
        )

    monkeypatch.setattr(toc_tool, "run_in_child_process", _fake_batch_render)
    monkeypatch.setattr(
        "app.services.document_agent.tools.vlm_toc_extractor.vlm_extract_toc_batch",
        _fake_batch,
    )

    regions = toc_tool._extract_regions_for_confirmed_anchors(  # noqa: SLF001
        confirmed,
        pdf_path="/tmp/doc.pdf",
        page_count=100,
        output_dir=str(tmp_path / "toc_pages"),
        dpi=72,
        model="fake-vlm",
    )

    assert len(vlm_rounds) == 1
    assert regions[0].toc_pages == [40]
    assert regions[0].entries == [
        {"title": "3.1 INTRODUCTION", "page_number": 1, "level": 1}
    ]
    assert regions[0].hierarchies
    assert regions[0].hierarchies[0]["toc_range"] == [40, 40]
    assert regions[0].batch_trace[0]["kept_toc_pages"] == [40]
    assert regions[0].batch_trace[0]["expanded"] is False


def test_extract_region_expands_only_on_full_unbroken_toc_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    confirmed = _anchors(tmp_path, [10])
    vlm_rounds: list[list[int]] = []

    def _fake_batch(
        *,
        page_pngs: list[tuple[int, str]],
        model: str,
        previous_entries: list[dict[str, Any]] | None = None,
    ) -> BatchTocResult:
        pages = [page for page, _ in page_pngs]
        vlm_rounds.append(pages)
        if pages[0] == 10:
            assert previous_entries in (None, [])
            return _batch_result(
                toc_pages=pages,
                non_toc_pages=[],
                page_results=[
                    BatchPageResult(
                        page=page,
                        is_toc=True,
                        entries=[
                            {
                                "title": f"Entry {page}",
                                "page_number": page,
                                "level": 1,
                            }
                        ],
                    )
                    for page in pages
                ],
            )
        return _batch_result(
            toc_pages=[pages[0]],
            non_toc_pages=pages[1:],
            page_results=[
                BatchPageResult(
                    page=pages[0],
                    is_toc=True,
                    entries=[
                        {
                            "title": f"Entry {pages[0]}",
                            "page_number": pages[0],
                            "level": 1,
                        }
                    ],
                ),
                *[
                    BatchPageResult(page=page, is_toc=False, entries=[])
                    for page in pages[1:]
                ],
            ],
        )

    monkeypatch.setattr(toc_tool, "run_in_child_process", _fake_batch_render)
    monkeypatch.setattr(
        "app.services.document_agent.tools.vlm_toc_extractor.vlm_extract_toc_batch",
        _fake_batch,
    )

    regions = toc_tool._extract_regions_for_confirmed_anchors(  # noqa: SLF001
        confirmed,
        pdf_path="/tmp/doc.pdf",
        page_count=100,
        output_dir=str(tmp_path / "toc_pages"),
        dpi=72,
        model="fake-vlm",
    )

    assert vlm_rounds == [
        [10, 11, 12, 13, 14],
        [15, 16, 17, 18, 19],
    ]
    assert regions[0].toc_pages == [10, 11, 12, 13, 14, 15]
    assert regions[0].batch_trace[0]["expanded"] is True
    assert regions[0].batch_trace[1]["expanded"] is False
