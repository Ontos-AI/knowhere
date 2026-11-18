"""Contract tests for nesting TOCs before calibration. No PDF, no VLM."""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.state import ProfileBlackboard
from app.services.document_agent.structure.anchoring_primitives import (
    SkeletonAnchor,
    deserialize_skeleton_anchor,
    serialize_skeleton_anchor,
)
from app.services.document_agent.structure.hierarchy_locator import TitleMatch
from app.services.document_agent.structure.toc_anchoring import (
    _assign_toc_relationships,
    _host_depth,
)
from app.services.document_agent.structure.toc_pairing import (
    PairedTocMatch,
    NestingAnswer,
    check_toc_nesting,
)
from app.services.document_agent.structure.toc_nesting import (
    PendingTocNesting,
    nest_pending_tocs,
)

_NO_ANSWER = None


def _toc(start: int, end: int | None = None) -> dict[str, Any]:
    return {"toc_range": [start, end or start], "toc_range_unit": "page"}


def _ctx(tmp_path=None) -> ToolContext:
    return ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="job-nesting",
        blackboard=ProfileBlackboard(page_count=100),
        trace=None,
        output_dir=str(tmp_path) if tmp_path else None,
        settings={"vlm_model": "fake-vlm"},
    )


def _fake_checks(
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[tuple[int, int], bool | None],
    *,
    paired: frozenset[tuple[int, int]] = frozenset(),
) -> list[tuple[int, int]]:
    """Answer each (TOC A start, TOC B start) pair; unlisted pairs are ``False``."""
    asked: list[tuple[int, int]] = []

    def nesting(toc_a, toc_b, **_kwargs) -> NestingAnswer:
        key = (toc_a["toc_range"][0], toc_b["toc_range"][0])
        asked.append(key)
        return NestingAnswer(inside=answers.get(key, False))

    def pair(toc_a, toc_b, **_kwargs) -> PairedTocMatch:
        key = (toc_a["toc_range"][0], toc_b["toc_range"][0])
        return PairedTocMatch(is_paired=key in paired, pattern=None)

    module = "app.services.document_agent.structure.toc_pairing"
    monkeypatch.setattr(f"{module}.check_toc_nesting", nesting)
    monkeypatch.setattr(f"{module}.check_paired_toc", pair)
    return asked


def test_nesting_uses_a_stack_and_pops_to_the_containing_toc(monkeypatch) -> None:
    primary = _toc(1)
    pending = [_toc(10), _toc(20), _toc(30), _toc(40)]
    asked = _fake_checks(
        monkeypatch,
        {(1, 10): True, (1, 20): True, (20, 30): True},
    )

    nesting = nest_pending_tocs(primary_toc=primary, pending=pending, ctx=_ctx())

    assert nesting.parents == {0: "root", 1: "root", 2: 1, 3: None}
    assert nesting.unknown == set()
    assert nesting.paired == {}
    # 20 is not inside 10, so 10 closes and 20 is asked against the primary;
    # 40 closes 30, 20 and the primary in turn.
    assert asked == [
        (1, 10),
        (10, 20),
        (1, 20),
        (20, 30),
        (30, 40),
        (20, 40),
        (1, 40),
    ]


def test_unanswered_check_leaves_the_toc_unknown_and_the_stack_as_it_was(
    monkeypatch,
) -> None:
    primary = _toc(1)
    pending = [_toc(10), _toc(20), _toc(30)]
    # 20: not inside 10, then no answer against the primary.
    asked = _fake_checks(
        monkeypatch,
        {(1, 10): True, (10, 20): False, (1, 20): _NO_ANSWER, (10, 30): True},
    )

    nesting = nest_pending_tocs(primary_toc=primary, pending=pending, ctx=_ctx())

    assert nesting.parents == {0: "root", 2: 0}
    assert nesting.unknown == {1}
    assert nesting.calibrated_indices() == [0, 1, 2]
    # The pop of 10 was not applied, so 30 is asked against 10 first.
    assert asked == [(1, 10), (10, 20), (1, 20), (10, 30)]


def test_paired_check_runs_against_the_latest_unpaired_toc(monkeypatch) -> None:
    primary = _toc(1, 6)
    pending = [_toc(7, 12), _toc(20), _toc(21)]
    asked = _fake_checks(
        monkeypatch,
        {(1, 20): _NO_ANSWER},
        paired=frozenset({(1, 7), (20, 21)}),
    )

    nesting = nest_pending_tocs(primary_toc=primary, pending=pending, ctx=_ctx())

    assert nesting.paired[0]["host"] == "root"
    assert nesting.paired[0]["paired_with_range"] == [1, 6]
    # 20 is unknown but still the latest unpaired TOC, so 21 pairs with it.
    assert nesting.unknown == {1}
    assert nesting.paired[2]["host"] == 1
    assert pending[2]["paired_with_range"] == [20, 20]
    assert asked == [(1, 20)]


def test_scope_ends_before_the_next_toc_that_is_not_a_sub_toc() -> None:
    pending = [_toc(10), _toc(20), _toc(30), _toc(40)]
    nesting = PendingTocNesting(parents={0: "root", 1: "root", 2: 1, 3: None})

    assert nesting.scope_end(0, pending=pending, page_count=100) == 19
    # 30 is a sub-TOC of 20, so it does not end 20's scope.
    assert nesting.scope_end(1, pending=pending, page_count=100) == 39
    assert nesting.scope_end(2, pending=pending, page_count=100) == 39
    assert nesting.scope_end(3, pending=pending, page_count=100) == 100


def test_unknown_tocs_end_scopes_and_paired_tocs_do_not() -> None:
    pending = [_toc(10), _toc(20), _toc(30), _toc(40)]
    nesting = PendingTocNesting(parents={0: "root", 3: 0}, unknown={2}, paired={1: {}})

    assert nesting.scope_end(0, pending=pending, page_count=100) == 29
    assert nesting.scope_end(2, pending=pending, page_count=100) == 39


def _span_anchor(start: int, end: int) -> dict[str, Any]:
    overrides = {
        (f"s{page}",): TitleMatch(
            page=page, source="anchored", matched_line="", candidates=[page]
        )
        for page in (start, end)
    }
    return serialize_skeleton_anchor(
        SkeletonAnchor(
            offset=0,
            offset_status="ok",
            match_overrides=overrides,
            null_page_report=[],
            bulk_count=len(overrides),
        )
    )


def _root_anchor(start: int, end: int) -> SkeletonAnchor:
    return deserialize_skeleton_anchor(_span_anchor(start, end))


def _calibrated(toc: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    return {"toc": toc, "nodes": [], "skeleton_anchor": _span_anchor(start, end)}


def _assign(
    nesting: PendingTocNesting,
    pending: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> None:
    _assign_toc_relationships(
        nesting=nesting,
        pending=pending,
        pending_records=records,
        root_anchor=_root_anchor(5, 90),
        root_toc=_toc(1),
    )


def test_nested_parents_skip_parents_without_a_forest() -> None:
    pending = [_toc(10), _toc(20), _toc(30), _toc(40), _toc(50)]
    nesting = PendingTocNesting(parents={0: "root", 1: 0, 2: 1, 3: None, 4: 3})
    # 20 failed calibration (no record); 40 anchored nothing.
    records = [
        _calibrated(pending[0], 11, 60),
        _calibrated(pending[2], 31, 35),
        {"toc": pending[3], "relationship": "unresolvable"},
        _calibrated(pending[4], 51, 55),
    ]

    _assign(nesting, pending, records)

    assert (records[0]["relationship"], records[0]["host"]) == ("contained", "root")
    # 30's parent 20 has no forest, so it moves up to 10 (record 0).
    assert (records[1]["relationship"], records[1]["host"]) == ("contained", 0)
    assert records[2]["relationship"] == "unresolvable"
    # 50's parent 40 has no forest and 40 sits at document level.
    assert records[3]["relationship"] == "parallel"
    assert "host" not in records[3]


def test_equal_span_is_paired_and_its_sub_tocs_move_up() -> None:
    pending = [_toc(10), _toc(20), _toc(30)]
    nesting = PendingTocNesting(parents={0: "root", 1: 0, 2: 1})
    records = [
        _calibrated(pending[0], 11, 60),
        # 20 anchors exactly where 10 does: a copy of 10.
        _calibrated(pending[1], 11, 60),
        _calibrated(pending[2], 31, 35),
    ]

    _assign(nesting, pending, records)

    assert records[1]["relationship"] == "paired"
    assert records[1]["host"] == 0
    assert records[1]["paired_with_range"] == [10, 10]
    assert pending[1]["paired_with_range"] == [10, 10]
    assert (records[2]["relationship"], records[2]["host"]) == ("contained", 0)


def test_equal_span_with_root_is_paired_with_root() -> None:
    pending = [_toc(10)]
    nesting = PendingTocNesting(parents={0: "root"})
    records = [_calibrated(pending[0], 5, 90)]

    _assign(nesting, pending, records)

    assert records[0]["relationship"] == "paired"
    assert records[0]["host"] == "root"
    assert records[0]["paired_with_range"] == [1, 1]


def test_unknown_toc_is_placed_by_evidenced_spans() -> None:
    pending = [_toc(10), _toc(20), _toc(95)]
    nesting = PendingTocNesting(parents={0: "root"}, unknown={1, 2})
    records = [
        _calibrated(pending[0], 11, 60),
        _calibrated(pending[1], 21, 30),
        _calibrated(pending[2], 96, 99),
    ]

    _assign(nesting, pending, records)

    assert (records[0]["relationship"], records[0]["host"]) == ("contained", "root")
    # Tightest span that contains 21-30 is 10's (11-60), not the root's.
    assert (records[1]["relationship"], records[1]["host"]) == ("contained", 0)
    # No span contains 96-99.
    assert records[2]["relationship"] == "parallel"


def test_host_depth_counts_pending_hosts() -> None:
    records: list[dict[str, Any]] = [
        {"relationship": "contained", "host": "root"},
        {"relationship": "contained", "host": 0},
        {"relationship": "contained", "host": 1},
        {"relationship": "parallel"},
    ]

    assert [_host_depth(records, index) for index in range(4)] == [0, 1, 2, 0]


def test_nesting_check_without_model_gives_no_answer() -> None:
    nesting = check_toc_nesting(_toc(1), _toc(10), ctx=_ctx(), model=None)
    assert nesting.inside is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"inside": true, "reason": "appendix contents"}', True),
        ('{"inside": false, "reason": "separate document"}', False),
        ('{"inside": "yes", "reason": "unsure"}', None),
        ("not json", None),
    ],
)
def test_nesting_check_parses_vlm_answer(tmp_path, monkeypatch, raw, expected) -> None:
    toc_dir = tmp_path / "toc_pages"
    toc_dir.mkdir()
    for page in (1, 10):
        (toc_dir / f"toc_a1_p{page}.png").write_bytes(b"png")

    class _Client:
        def chat_completion_with_usage(self, **kwargs):
            assert kwargs["usage_task"] == "document_agent.toc_nesting_check"
            assert kwargs["temperature"] == 0.0
            return raw, {}

    monkeypatch.setattr(
        "shared.services.ai.llm_overrides.get_vision_client",
        lambda requested_model=None: (_Client(), "fake-vlm"),
    )

    nesting = check_toc_nesting(_toc(1), _toc(10), ctx=_ctx(tmp_path), model="fake-vlm")

    assert nesting.inside is expected
