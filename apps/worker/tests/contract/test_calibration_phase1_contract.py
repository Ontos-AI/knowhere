"""Contract tests for deterministic calibration Phase-1 (regimes + scan)."""

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

from app.services.document_agent.calibration import phase1 as phase1_module
from app.services.document_agent.calibration.phase1 import (
    PROBES_PER_REGIME,
    run_calibration_phase1,
)
from app.services.document_agent.calibration.scan import TitleScanResult
from app.services.document_agent.calibration.types import (
    FAILURE_NO_OFFSET,
    FAILURE_PAGE_COUNT_MISSING,
    FAILURE_TOC_EMPTY,
)
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.state import ProfileBlackboard


def _ctx(page_count: int = 60) -> ToolContext:
    blackboard = ProfileBlackboard()
    blackboard.page_count = page_count
    return ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="job-phase1",
        blackboard=blackboard,
        trace=None,
        settings={"vlm_model": "test-vlm"},
    )


def _hierarchy(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"toc_range": [1, 3], "toc_with_level": entries}]


class _FakeScan:
    """Answers each title from ``hits``; records probe order."""

    def __init__(self, hits: dict[str, int]) -> None:
        self.hits = hits
        self.calls: list[tuple[str, int]] = []

    def __call__(
        self,
        *,
        ctx: ToolContext,
        title: str,
        start_page: int,
        page_count: int,
        **kwargs: Any,
    ) -> TitleScanResult:
        self.calls.append((title, start_page))
        found_page = self.hits.get(title)
        return TitleScanResult(
            title=title,
            found=found_page is not None,
            found_page=found_page,
            scanned_pages=[start_page],
            next_start=start_page + 1,
        )


@pytest.fixture
def patch_scan(monkeypatch: pytest.MonkeyPatch):
    def _apply(fake: _FakeScan) -> _FakeScan:
        monkeypatch.setattr(phase1_module, "scan_title_forward", fake)
        return fake

    return _apply


def test_offset_is_found_page_minus_printed(patch_scan) -> None:
    fake = patch_scan(_FakeScan({"Chapter 1": 15}))
    hierarchies = _hierarchy(
        [
            {"heading": "Chapter 1", "page_number": "10", "level": 1},
            {"heading": "Chapter 2", "page_number": "20", "level": 1},
        ]
    )

    result = run_calibration_phase1(
        ctx=_ctx(), toc_hierarchies=hierarchies, page_count=60
    )

    assert result.status == "ok"
    assert [(r.kind, r.offset) for r in result.regimes] == [("decimal", 5)]
    assert fake.calls == [("Chapter 1", 10)]


def test_first_hit_stops_the_regime(patch_scan) -> None:
    fake = patch_scan(_FakeScan({"Chapter 1": 15, "Chapter 2": 25}))
    hierarchies = _hierarchy(
        [
            {"heading": "Chapter 1", "page_number": "10", "level": 1},
            {"heading": "Chapter 2", "page_number": "20", "level": 1},
        ]
    )

    run_calibration_phase1(ctx=_ctx(), toc_hierarchies=hierarchies, page_count=60)

    assert fake.calls == [("Chapter 1", 10)]


def test_second_probe_runs_when_the_first_misses(patch_scan) -> None:
    fake = patch_scan(_FakeScan({"Chapter 2": 25}))
    hierarchies = _hierarchy(
        [
            {"heading": "Chapter 1", "page_number": "10", "level": 1},
            {"heading": "Chapter 2", "page_number": "20", "level": 1},
            {"heading": "Chapter 3", "page_number": "30", "level": 1},
        ]
    )

    result = run_calibration_phase1(
        ctx=_ctx(), toc_hierarchies=hierarchies, page_count=60
    )

    assert fake.calls == [("Chapter 1", 10), ("Chapter 2", 20)]
    assert [r.offset for r in result.regimes] == [5]


def test_probe_count_per_regime_is_capped(patch_scan) -> None:
    fake = patch_scan(_FakeScan({}))
    hierarchies = _hierarchy(
        [
            {"heading": f"Chapter {i}", "page_number": str(i), "level": 1}
            for i in range(1, 8)
        ]
    )

    result = run_calibration_phase1(
        ctx=_ctx(), toc_hierarchies=hierarchies, page_count=60
    )

    assert len(fake.calls) == PROBES_PER_REGIME
    assert result.status == "failed"
    assert result.failure_kind == FAILURE_NO_OFFSET


def test_roman_and_decimal_regimes_calibrate_independently(patch_scan) -> None:
    fake = patch_scan(_FakeScan({"Preface": 4, "Chapter 1": 15}))
    hierarchies = _hierarchy(
        [
            {"heading": "Preface", "page_number": "ii", "level": 1},
            {"heading": "Foreword", "page_number": "iv", "level": 1},
            {"heading": "Chapter 1", "page_number": "10", "level": 1},
            {"heading": "Chapter 2", "page_number": "20", "level": 1},
        ]
    )

    result = run_calibration_phase1(
        ctx=_ctx(), toc_hierarchies=hierarchies, page_count=60
    )

    assert {(r.kind, r.offset) for r in result.regimes} == {
        ("roman", 2),
        ("decimal", 5),
    }
    assert fake.calls == [("Preface", 2), ("Chapter 1", 10)]


def test_confirmed_anchor_is_reported_as_a_sample(patch_scan) -> None:
    patch_scan(_FakeScan({"Chapter 1": 15}))
    hierarchies = _hierarchy(
        [{"heading": "Chapter 1", "page_number": "10", "level": 1}]
    )

    result = run_calibration_phase1(
        ctx=_ctx(), toc_hierarchies=hierarchies, page_count=60
    )

    sample = result.regimes[0].samples[0]
    assert (sample.title, sample.physical) == ("Chapter 1", 15)


def test_entries_without_a_parseable_printed_page_are_skipped(patch_scan) -> None:
    fake = patch_scan(_FakeScan({"Chapter 1": 15}))
    hierarchies = _hierarchy(
        [
            {"heading": "Cover", "page_number": None, "level": 1},
            {"heading": "Chapter 1", "page_number": "10", "level": 1},
        ]
    )

    run_calibration_phase1(ctx=_ctx(), toc_hierarchies=hierarchies, page_count=60)

    assert fake.calls == [("Chapter 1", 10)]


def test_empty_toc_fails_without_scanning(patch_scan) -> None:
    fake = patch_scan(_FakeScan({}))

    result = run_calibration_phase1(ctx=_ctx(), toc_hierarchies=[], page_count=60)

    assert result.failure_kind == FAILURE_TOC_EMPTY
    assert fake.calls == []


def test_missing_page_count_fails_without_scanning(patch_scan) -> None:
    fake = patch_scan(_FakeScan({}))
    hierarchies = _hierarchy(
        [{"heading": "Chapter 1", "page_number": "10", "level": 1}]
    )

    result = run_calibration_phase1(
        ctx=_ctx(page_count=0), toc_hierarchies=hierarchies, page_count=0
    )

    assert result.failure_kind == FAILURE_PAGE_COUNT_MISSING
    assert fake.calls == []
