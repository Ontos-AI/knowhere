"""Contract tests for the deterministic forward title scan (calibration Phase-1)."""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from app.services.document_agent.calibration import scan as scan_module
from app.services.document_agent.calibration.scan import (
    DEFAULT_WINDOW_SCHEDULE,
    progressive_page_windows,
    scan_title_forward,
)
from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.state import ProfileBlackboard


def _ctx(page_count: int = 60) -> ToolContext:
    blackboard = ProfileBlackboard()
    blackboard.page_count = page_count
    return ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="job-scan",
        blackboard=blackboard,
        trace=None,
        settings={"vlm_model": "test-vlm"},
    )


class _FakeInspect:
    """Records every inspect call and answers hit only on ``hit_page``."""

    def __init__(self, hit_page: int | None) -> None:
        self.hit_page = hit_page
        self.calls: list[list[int]] = []

    def __call__(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        pages = list(args.get("pages") or [])
        self.calls.append(pages)
        hit = self.hit_page in pages if self.hit_page is not None else False
        return ToolResult(
            status="ok",
            payload={
                "pages": pages,
                "answer": "",
                "fields": {
                    "found": hit,
                    "reason": "hit" if hit else "miss",
                },
            },
        )


def _fake_render_pages(
    ctx: ToolContext,
    pages: list[int],
    **kwargs: Any,
) -> list[dict[str, Any]]:
    return [
        {"page": int(page), "png_path": f"/tmp/scan_page_{int(page)}.png"}
        for page in pages
    ]


@pytest.fixture
def patch_inspect(monkeypatch: pytest.MonkeyPatch):
    def _apply(fake: _FakeInspect) -> _FakeInspect:
        monkeypatch.setattr(scan_module, "inspect_pages", fake)
        monkeypatch.setattr(scan_module, "render_pages", _fake_render_pages)
        return fake

    return _apply


def test_progressive_page_windows_are_non_overlapping_and_clipped() -> None:
    assert progressive_page_windows(start_page=10, end_page=20) == [
        [10, 11],
        [12, 13, 14, 15],
        [16, 17, 18, 19, 20],
    ]


def test_first_round_opens_the_candidate_page_and_its_successor(patch_inspect) -> None:
    fake = patch_inspect(_FakeInspect(hit_page=10))

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert sorted(fake.calls) == [[10], [11]]
    assert all(len(call) == 1 for call in fake.calls)
    assert result.found is True
    assert result.found_page == 10


def test_miss_expands_forward_from_the_cursor_without_rescanning(patch_inspect) -> None:
    fake = patch_inspect(_FakeInspect(hit_page=None))

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert sorted(fake.calls) == [[page] for page in range(10, 32)]
    assert all(len(call) == 1 for call in fake.calls)
    assert result.found is False
    assert result.scanned_pages == list(range(10, 32))
    assert len(result.scanned_pages) == len(set(result.scanned_pages))
    assert result.next_start == 32


def test_scan_stops_at_first_hit(patch_inspect) -> None:
    fake = patch_inspect(_FakeInspect(hit_page=13))

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert sorted(fake.calls) == [[10], [11], [12], [13], [14], [15]]
    assert result.found_page == 13
    assert result.next_start == 16


def test_scan_covers_at_most_the_window_schedule(patch_inspect) -> None:
    fake = patch_inspect(_FakeInspect(hit_page=None))

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert len(fake.calls) == sum(DEFAULT_WINDOW_SCHEDULE)
    assert len(result.scanned_pages) == sum(DEFAULT_WINDOW_SCHEDULE)


def test_window_is_clipped_at_the_last_page(patch_inspect) -> None:
    fake = patch_inspect(_FakeInspect(hit_page=None))

    result = scan_title_forward(
        ctx=_ctx(page_count=13), title="Appendix", start_page=10, page_count=13
    )

    assert sorted(fake.calls) == [[10], [11], [12], [13]]
    assert result.next_start is None


def test_each_call_is_single_page_with_page_cap_one(patch_inspect) -> None:
    class _Recording(_FakeInspect):
        def __init__(self) -> None:
            super().__init__(hit_page=None)
            self.caps: list[int] = []

        def __call__(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
            self.caps.append(int(args["page_cap"]))
            return super().__call__(ctx, args)

    fake = patch_inspect(_Recording())

    scan_title_forward(ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60)

    assert fake.caps == [1] * sum(DEFAULT_WINDOW_SCHEDULE)
    assert all(len(call) == 1 for call in fake.calls)


def test_inspect_error_continues_to_later_windows(patch_inspect) -> None:
    class _FailThenHit(_FakeInspect):
        def __call__(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
            pages = list(args.get("pages") or [])
            self.calls.append(pages)
            page = pages[0] if pages else None
            if page is not None and page <= 11:
                return ToolResult(
                    status="error", error="calibration visual budget exhausted"
                )
            hit = page == 13
            return ToolResult(
                status="ok",
                payload={
                    "fields": {
                        "found": hit,
                        "reason": "hit" if hit else "miss",
                    }
                },
            )

    fake = patch_inspect(_FailThenHit(hit_page=None))

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert sorted(fake.calls) == [[10], [11], [12], [13], [14], [15]]
    assert result.found is True
    assert result.found_page == 13
    assert result.rounds[0].error == "calibration visual budget exhausted"


def test_string_false_is_not_treated_as_a_hit(patch_inspect) -> None:
    class _StringBool(_FakeInspect):
        def __call__(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
            pages = list(args.get("pages") or [])
            self.calls.append(pages)
            return ToolResult(
                status="ok",
                payload={
                    "fields": {
                        "found": "false",
                        "reason": "no",
                    }
                },
            )

    fake = patch_inspect(_StringBool(hit_page=None))

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert result.found is False
    assert result.found_page is None
    assert len(fake.calls) == sum(DEFAULT_WINDOW_SCHEDULE)


def test_earliest_true_page_wins_within_a_round(patch_inspect) -> None:
    class _MultiHit(_FakeInspect):
        def __call__(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
            pages = list(args.get("pages") or [])
            self.calls.append(pages)
            page = pages[0] if pages else None
            hit = page in {13, 14}
            return ToolResult(
                status="ok",
                payload={
                    "fields": {
                        "found": hit,
                        "reason": "hit" if hit else "miss",
                    }
                },
            )

    fake = patch_inspect(_MultiHit(hit_page=None))

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert result.found_page == 13
    assert sorted(fake.calls) == [[10], [11], [12], [13], [14], [15]]


def test_window_renders_once_serially_before_concurrent_vlm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PDF→PNG is one serial batch per window; VLM stays one page per call."""
    render_calls: list[list[int]] = []
    inspect_args: list[dict[str, Any]] = []

    def _recording_render(
        ctx: ToolContext,
        pages: list[int],
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        render_calls.append(list(pages))
        return _fake_render_pages(ctx, pages, **kwargs)

    def _capture_inspect(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        inspect_args.append(dict(args))
        pages = list(args.get("pages") or [])
        hit = 10 in pages
        return ToolResult(
            status="ok",
            payload={
                "pages": pages,
                "answer": "",
                "fields": {
                    "found": hit,
                    "reason": "hit" if hit else "miss",
                },
            },
        )

    monkeypatch.setattr(scan_module, "inspect_pages", _capture_inspect)
    monkeypatch.setattr(scan_module, "render_pages", _recording_render)

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert result.found_page == 10
    assert render_calls == [[10, 11]]
    assert len(inspect_args) == 2
    assert sorted(args["pages"] for args in inspect_args) == [[10], [11]]
    for args in inspect_args:
        assert args["page_cap"] == 1
        assert len(args["rendered_pages"]) == 1
        assert args["rendered_pages"][0]["page"] == args["pages"][0]
