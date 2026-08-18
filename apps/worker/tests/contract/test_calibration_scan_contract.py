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
    scan_title_forward,
)
from app.services.document_agent.budget import BudgetTracker
from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.state import ProfileBlackboard


def _ctx(page_count: int = 60) -> ToolContext:
    blackboard = ProfileBlackboard()
    blackboard.page_count = page_count
    return ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="job-scan",
        blackboard=blackboard,
        budget=BudgetTracker(plan_budget=50_000, visual_budget=80_000),
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
                    "found_page": self.hit_page if hit else None,
                },
            },
        )


@pytest.fixture
def patch_inspect(monkeypatch: pytest.MonkeyPatch):
    def _apply(fake: _FakeInspect) -> _FakeInspect:
        monkeypatch.setattr(scan_module, "inspect_pages", fake)
        return fake

    return _apply


def test_first_round_opens_the_candidate_page_and_its_successor(patch_inspect) -> None:
    fake = patch_inspect(_FakeInspect(hit_page=10))

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert fake.calls == [[10, 11]]
    assert result.found is True
    assert result.found_page == 10


def test_miss_expands_forward_from_the_cursor_without_rescanning(patch_inspect) -> None:
    fake = patch_inspect(_FakeInspect(hit_page=None))

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert fake.calls == [
        [10, 11],
        [12, 13, 14, 15],
        [16, 17, 18, 19, 20, 21],
        [22, 23, 24, 25, 26, 27, 28, 29, 30, 31],
    ]
    assert result.found is False
    assert result.scanned_pages == list(range(10, 32))
    assert len(result.scanned_pages) == len(set(result.scanned_pages))
    assert result.next_start == 32


def test_scan_stops_at_first_hit(patch_inspect) -> None:
    fake = patch_inspect(_FakeInspect(hit_page=13))

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert fake.calls == [[10, 11], [12, 13, 14, 15]]
    assert result.found_page == 13
    assert result.next_start == 16


def test_scan_covers_at_most_the_window_schedule(patch_inspect) -> None:
    fake = patch_inspect(_FakeInspect(hit_page=None))

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert len(fake.calls) == len(DEFAULT_WINDOW_SCHEDULE)
    assert len(result.scanned_pages) == sum(DEFAULT_WINDOW_SCHEDULE)


def test_window_is_clipped_at_the_last_page(patch_inspect) -> None:
    fake = patch_inspect(_FakeInspect(hit_page=None))

    result = scan_title_forward(
        ctx=_ctx(page_count=13), title="Appendix", start_page=10, page_count=13
    )

    assert fake.calls == [[10, 11], [12, 13]]
    assert result.next_start is None


def test_each_call_lifts_the_page_cap_to_its_own_window(patch_inspect) -> None:
    class _Recording(_FakeInspect):
        def __init__(self) -> None:
            super().__init__(hit_page=None)
            self.caps: list[int] = []

        def __call__(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
            self.caps.append(int(args["page_cap"]))
            return super().__call__(ctx, args)

    fake = patch_inspect(_Recording())

    scan_title_forward(ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60)

    assert fake.caps == list(DEFAULT_WINDOW_SCHEDULE)


def test_inspect_error_aborts_the_scan(patch_inspect) -> None:
    class _Failing(_FakeInspect):
        def __call__(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
            self.calls.append(list(args.get("pages") or []))
            return ToolResult(status="error", error="calibration visual budget exhausted")

    fake = patch_inspect(_Failing(hit_page=None))

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert fake.calls == [[10, 11]]
    assert result.found is False
    assert result.rounds[-1].error == "calibration visual budget exhausted"


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
                        "found_page": pages[0] if pages else None,
                    }
                },
            )

    fake = patch_inspect(_StringBool(hit_page=None))

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert result.found is False
    assert result.found_page is None
    assert len(fake.calls) == len(DEFAULT_WINDOW_SCHEDULE)


def test_found_page_outside_the_window_is_rejected(patch_inspect) -> None:
    class _Liar(_FakeInspect):
        def __call__(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
            pages = list(args.get("pages") or [])
            self.calls.append(pages)
            return ToolResult(
                status="ok",
                payload={"fields": {"found": True, "found_page": 999}},
            )

    patch_inspect(_Liar(hit_page=None))

    result = scan_title_forward(
        ctx=_ctx(), title="Chapter 1", start_page=10, page_count=60
    )

    assert result.found is False
    assert result.found_page is None
