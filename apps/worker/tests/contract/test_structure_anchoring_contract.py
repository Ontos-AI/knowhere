"""Contract tests for hierarchy anchoring (Phase-2) + calibrate wiring."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from unittest.mock import patch
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
from app.services.document_agent.structure.hierarchy_locator import TitleMatch, TitleNode
from app.services.document_agent.structure import anchoring_primitives as anchoring


@pytest.fixture(autouse=True)
def _rebind_live_anchoring_primitives() -> Iterator[None]:
    """Rebind after contract fixtures that clear ``app.*`` from ``sys.modules``."""
    global anchoring
    from app.services.document_agent.structure import anchoring_primitives as live

    anchoring = live
    yield


@contextmanager
def _patch_verify(fake_verify: Callable[..., dict[str, Any]]) -> Iterator[None]:
    """Patch verify on the live anchoring module (not a collection-time zombie).

    Contract fixtures that call ``clear_application_modules()`` drop ``app.*`` from
    ``sys.modules``. Resolve the patch target by dotted path at enter time so it
    always hits the live globals closed over by ``_vlm_confirm_single_page``.
    """
    with patch(
        "app.services.document_agent.structure.anchoring_primitives.verify_section_page_choice",
        fake_verify,
    ):
        yield


def _ctx() -> ToolContext:
    return ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="job-anchor",
        blackboard=ProfileBlackboard(),
        trace=None,
        settings={"vlm_model": "test-vlm"},
    )


def _leaf(title: str, page: int) -> TitleNode:
    return TitleNode(title=title, level=1, printed_page=page, children=[])


def test_prune_out_of_scope_nodes_removes_overflow_leaves() -> None:
    nodes = [
        _leaf("A", 1),
        _leaf("B", 50),
    ]
    pruned, removed = anchoring.prune_out_of_scope_nodes(
        nodes, offset=0, page_count=10
    )
    assert removed == 1
    assert [n.title for n in pruned] == ["A"]


def test_null_page_parent_skipped_without_right_anchor() -> None:
    parent = TitleNode(
        title="Chapter",
        level=1,
        printed_page=None,
        children=[TitleNode(title="Orphan", level=2, printed_page=None, children=[])],
    )
    overrides, report = anchoring.locate_null_page_parent_overrides(
        nodes=[parent],
        match_overrides={},
        page_texts={1: "Chapter\nHello"},
        body_pages=[1, 2, 3],
        ctx=None,
    )
    assert overrides == {}
    assert len(report) == 1
    assert report[0]["result"] == "skipped_no_right"


def test_null_page_parent_located_via_normalized_text() -> None:
    child = TitleNode(title="1.1 Detail", level=2, printed_page=5, children=[])
    parent = TitleNode(
        title="1 Overview",
        level=1,
        printed_page=None,
        children=[child],
    )
    leaf_match = anchoring.bulk_offset_matches(
        [(("1 Overview", "1.1 Detail"), child)],
        offset=0,
    )
    page_texts = {
        4: "noise",
        5: "1 Overview\n1.1 Detail\nbody",
        6: "more",
    }
    overrides, report = anchoring.locate_null_page_parent_overrides(
        nodes=[parent],
        match_overrides=leaf_match,
        page_texts=page_texts,
        body_pages=[4, 5, 6],
        ctx=None,
    )
    assert ("1 Overview",) in overrides
    assert overrides[("1 Overview",)].page == 5
    assert report[0]["result"] != "unresolved"
    assert report[0]["page"] == 5
    assert report[0]["window"] == [1, 5]


def test_normalized_title_match_preserves_english_word_boundary() -> None:
    from app.services.document_agent.structure.hierarchy_locator import (
        locate_title_normalized_strict,
    )

    match = locate_title_normalized_strict(
        "附录 A OVERVIEW",
        scope_pages=[7],
        page_texts={7: "附录\nA   OVERVIEW"},
    )

    assert match is not None
    assert match.page == 7
    assert match.matched_line == "附录a overview"
    assert match.evidence["accept"] == "normalized_strict_unique"


def test_first_sibling_null_parent_uses_scan_forward_not_wide_rtl() -> None:
    """No left sibling: miss text → ``scan_title_forward`` within 2+4+6+10 budget."""
    child = TitleNode(title="22.1 Intro", level=2, printed_page=278, children=[])
    parent = TitleNode(
        title="Chapter 22",
        level=1,
        printed_page=None,
        children=[child],
    )
    leaf_match = {
        ("Chapter 22", "22.1 Intro"): TitleMatch(
            page=278,
            source="test",
            matched_line="",
            candidates=[278],
            evidence={},
        )
    }
    body_pages = list(range(1, 301))
    page_texts = {page: "noise" for page in body_pages}
    ctx = _ctx()

    scanned_starts: list[int] = []

    def fake_scan(**kwargs: Any) -> Any:
        from app.services.document_agent.calibration.scan import TitleScanResult

        scanned_starts.append(int(kwargs["start_page"]))
        assert int(kwargs["page_count"]) == 278
        assert int(kwargs["start_page"]) == anchoring._first_sibling_null_parent_scan_start(
            278
        )
        return TitleScanResult(
            title=str(kwargs["title"]),
            found=True,
            found_page=270,
            scanned_pages=list(range(int(kwargs["start_page"]), 271)),
            next_start=271,
        )

    with patch(
        "app.services.document_agent.calibration.scan.scan_title_forward",
        side_effect=fake_scan,
    ):
        with patch.object(anchoring, "_visual_rtl_locate_parent") as rtl:
            overrides, report = anchoring.locate_null_page_parent_overrides(
                nodes=[parent],
                match_overrides=leaf_match,
                page_texts=page_texts,
                body_pages=body_pages,
                ctx=ctx,
            )
            rtl.assert_not_called()

    assert scanned_starts == [anchoring._first_sibling_null_parent_scan_start(278)]
    assert overrides[("Chapter 22",)].page == 270
    assert report[0]["accept"] == "scan_forward"
    assert report[0]["window"] == [
        anchoring._first_sibling_null_parent_scan_start(278),
        278,
    ]


def test_null_page_parent_with_left_sibling_still_uses_rtl() -> None:
    left_child = TitleNode(title="A.1", level=2, printed_page=10, children=[])
    left = TitleNode(title="A", level=1, printed_page=10, children=[left_child])
    right_child = TitleNode(title="B.1", level=2, printed_page=50, children=[])
    right = TitleNode(title="B", level=1, printed_page=None, children=[right_child])
    overrides_in = {
        ("A",): TitleMatch(
            page=10,
            source="test",
            matched_line="",
            candidates=[10],
            evidence={},
        ),
        ("A", "A.1"): TitleMatch(
            page=10,
            source="test",
            matched_line="",
            candidates=[10],
            evidence={},
        ),
        ("B", "B.1"): TitleMatch(
            page=50,
            source="test",
            matched_line="",
            candidates=[50],
            evidence={},
        ),
    }
    page_texts = {p: "noise" for p in range(1, 61)}
    ctx = _ctx()

    def fake_rtl(**kwargs: Any) -> tuple[TitleMatch, int]:
        assert kwargs["left"] == 10
        assert kwargs["right"] == 50
        return (
            TitleMatch(
                page=40,
                source="inspect_vlm",
                matched_line="",
                candidates=[40],
                evidence={"accept": "visual_rtl"},
            ),
            3,
        )

    with patch(
        "app.services.document_agent.calibration.scan.scan_title_forward"
    ) as scan:
        with patch.object(
            anchoring, "_visual_rtl_locate_parent", side_effect=fake_rtl
        ):
            overrides, report = anchoring.locate_null_page_parent_overrides(
                nodes=[left, right],
                match_overrides=overrides_in,
                page_texts=page_texts,
                body_pages=list(range(1, 61)),
                ctx=ctx,
            )
        scan.assert_not_called()

    assert overrides[("B",)].page == 40
    assert report[0]["accept"] == "visual_rtl"


def test_phase2_bulk_via_mocked_offset() -> None:
    leaves = [
        _leaf("Intro", 3),
        _leaf("Body", 10),
        _leaf("End", 20),
    ]
    ctx = _ctx()
    seed = {
        ("Intro",): TitleMatch(
            page=5,
            source="inspect_vlm",
            matched_line="",
            candidates=[5],
            evidence={"calibration": True, "printed_page": 3},
        )
    }

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        expected = kwargs["candidate_matches"][0].page
        return {"selected_page": expected, "reason": "ok"}

    with _patch_verify(fake_verify):
        matches = anchoring.offset_guided_anchoring(
            nodes=leaves,
            offset=2,
            ctx=ctx,
            page_count=30,
            calibration_overrides=seed,
        )
    assert matches is not None
    assert len(matches) >= 3
    assert matches[("Intro",)].page == 5
    assert matches[("Body",)].page == 12
    assert matches[("End",)].page == 22


def test_anchor_hierarchy_uses_calibration_phase1() -> None:
    leaves = [_leaf("Only", 2)]
    toc_hierarchies = [
        {
            "toc_range": [1, 1],
            "toc_with_level": [
                {"heading": "Only", "level": 1, "page_number": 2},
            ],
        }
    ]
    ctx = _ctx()

    # Contract conftest evicts cached ``app.*`` modules, so resolve the live
    # orchestrator inside the test rather than at import time.
    from app.services.document_agent.calibration.orchestrator import (
        anchor_hierarchy,
    )
    from app.services.document_agent.calibration.types import (
        CalibrationRegime,
        CalibrationResult,
        CalibrationSample,
    )

    phase1 = CalibrationResult(
        status="ok",
        offset=2,
        offset_status="ok",
        regimes=[
            CalibrationRegime(
                kind="decimal",
                offset=2,
                offset_status="ok",
                entry_indices=[0],
                samples=[
                    CalibrationSample(title="Only", physical=4)
                ],
            )
        ],
    )

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        expected = kwargs["candidate_matches"][0].page
        return {"selected_page": expected, "reason": "ok"}

    with (
        patch(
            "app.services.document_agent.calibration.service.calibrate_offset",
            return_value=phase1,
        ),
        _patch_verify(fake_verify),
    ):
        nodes, anchor = anchor_hierarchy(
            nodes=leaves,
            toc_hierarchies=toc_hierarchies,
            page_texts={4: "Only\ntext"},
            body_pages=[2, 3, 4, 5],
            page_count=10,
            ctx=ctx,
        )
    # Duck-type: contract conftest can leave a stale SkeletonAnchor class identity.
    assert anchor.offset == 2
    assert anchor.offset_status == "ok"
    assert isinstance(anchor.match_overrides, dict)
    assert isinstance(anchor.null_page_report, list)
    assert isinstance(anchor.bulk_count, int)
    assert isinstance(anchor.pruned_count, int)
    assert nodes
    assert ("Only",) in anchor.match_overrides
    assert anchor.match_overrides[("Only",)].page == 4


def test_prune_unanchored_suffix_removes_toc_leaves() -> None:
    """Leaves without match_overrides are dropped (suffix = no TOC)."""
    nodes = [
        _leaf("A", 1),
        _leaf("B", 10),
        _leaf("C", 20),
        _leaf("D", 30),
    ]
    overrides = anchoring.bulk_offset_matches(
        [(("A",), nodes[0]), (("B",), nodes[1])],
        offset=5,
    )
    pruned, removed = anchoring.prune_unanchored_toc_leaves(
        nodes, match_overrides=overrides
    )
    assert removed == 2
    assert [n.title for n in pruned] == ["A", "B"]
    assert ("A",) in overrides and ("B",) in overrides


def test_bisect_all_fail_returns_minus_one() -> None:
    """No confirmed leaf under the offset ⇒ breakpoint is -1, not index 0."""
    leaves = [
        (("C1",), _leaf("C1", 1)),
        (("C2",), _leaf("C2", 2)),
        (("C3",), _leaf("C3", 3)),
    ]

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        return {"selected_page": None, "reason": "miss"}

    with _patch_verify(fake_verify):
        bp = anchoring._bisect_offset_breakpoint(
            leaves=leaves,
            offset=0,
            ctx=_ctx(),
            page_count=10,
        )
    assert bp == -1


def test_phase2_all_bisect_fail_does_not_invent_first_leaf() -> None:
    """When every Phase-2 probe fails, do not bulk-anchor the first TOC leaf."""
    from app.services.document_agent.calibration.procedure import (
        anchor_hierarchy_from_regimes,
    )
    from app.services.document_agent.calibration.scan import TitleScanResult
    from app.services.document_agent.calibration.types import (
        CalibrationRegime,
        CalibrationResult,
        CalibrationSample,
    )

    leaves = [
        _leaf("Ch1", 1),
        _leaf("Ch2", 5),
        _leaf("Ch3", 20),
    ]
    # Phase-1 sample is a different title so Ch1 is not protected by seed.
    phase1 = CalibrationResult(
        status="ok",
        offset=10,
        regimes=[
            CalibrationRegime(
                kind="decimal",
                offset=10,
                offset_status="ok",
                entry_indices=[0, 1, 2],
                samples=[CalibrationSample(title="Other", physical=99)],
            )
        ],
    )
    ctx = _ctx()

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        return {"selected_page": None, "reason": "miss"}

    def fake_scan(**kwargs: Any) -> TitleScanResult:
        return TitleScanResult(
            title=str(kwargs.get("title") or ""),
            found=False,
            found_page=None,
            scanned_pages=[],
            next_start=None,
        )

    with (
        _patch_verify(fake_verify),
        patch(
            "app.services.document_agent.calibration.scan.scan_title_forward",
            fake_scan,
        ),
    ):
        working, anchor = anchor_hierarchy_from_regimes(
            nodes=leaves,
            result=phase1,
            entries=[
                {"heading": "Ch1", "level": 1, "page_number": 1},
                {"heading": "Ch2", "level": 1, "page_number": 5},
                {"heading": "Ch3", "level": 1, "page_number": 20},
            ],
            page_texts={11: "noise", 15: "noise", 30: "noise"},
            body_pages=list(range(1, 50)),
            page_count=50,
            ctx=ctx,
        )

    assert working == []
    assert ("Ch1",) not in anchor.match_overrides
    assert ("Ch2",) not in anchor.match_overrides
    assert ("Ch3",) not in anchor.match_overrides
    assert anchor.pruned_count >= 3


def test_phase2_recalibrate_uses_forward_scan_beyond_plus_five() -> None:
    """Breakpoint suffix reuses Phase-1 forward scan (not a +1..+5 grid)."""
    from app.services.document_agent.calibration.procedure import (
        anchor_hierarchy_from_regimes,
    )
    from app.services.document_agent.calibration.scan import TitleScanResult
    from app.services.document_agent.calibration.types import (
        CalibrationRegime,
        CalibrationResult,
        CalibrationSample,
    )

    leaves = [
        _leaf("Ch1", 1),
        _leaf("Ch2", 5),
        _leaf("Ch3", 20),
        _leaf("Ch4", 30),
    ]
    phase1 = CalibrationResult(
        status="ok",
        offset=10,
        regimes=[
            CalibrationRegime(
                kind="decimal",
                offset=10,
                offset_status="ok",
                entry_indices=[0, 1, 2, 3],
                samples=[CalibrationSample(title="Ch1", physical=11)],
            )
        ],
    )
    ctx = _ctx()

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        expected = int(kwargs["candidate_matches"][0].page)
        title = str(kwargs.get("title") or "")
        # Prefix at offset=10; after forward-scan recalibrate, suffix uses +16.
        ok = {
            ("Ch1", 11),
            ("Ch2", 15),
            ("Ch3", 36),  # 20+16
            ("Ch4", 46),  # 30+16
        }
        if (title, expected) in ok:
            return {"selected_page": expected, "reason": "ok"}
        return {"selected_page": None, "reason": "miss"}

    def fake_scan(**kwargs: Any) -> TitleScanResult:
        title = str(kwargs.get("title") or "")
        start = int(kwargs.get("start_page") or 0)
        # Old slot for Ch3 was page 30; scan starts at 31 and finds 36 → offset 16.
        assert title == "Ch3"
        assert start == 31
        return TitleScanResult(
            title=title,
            found=True,
            found_page=36,
            scanned_pages=[31, 32, 33, 34, 35, 36],
            next_start=37,
        )

    with (
        _patch_verify(fake_verify),
        patch(
            "app.services.document_agent.calibration.scan.scan_title_forward",
            fake_scan,
        ),
    ):
        working, anchor = anchor_hierarchy_from_regimes(
            nodes=leaves,
            result=phase1,
            entries=[
                {"heading": "Ch1", "level": 1, "page_number": 1},
                {"heading": "Ch2", "level": 1, "page_number": 5},
                {"heading": "Ch3", "level": 1, "page_number": 20},
                {"heading": "Ch4", "level": 1, "page_number": 30},
            ],
            page_texts={11: "Ch1", 15: "Ch2", 36: "Ch3", 46: "Ch4"},
            body_pages=list(range(1, 60)),
            page_count=60,
            ctx=ctx,
        )

    titles = [n.title for n in working]
    assert titles == ["Ch1", "Ch2", "Ch3", "Ch4"]
    assert anchor.match_overrides[("Ch1",)].page == 11
    assert anchor.match_overrides[("Ch2",)].page == 15
    assert anchor.match_overrides[("Ch3",)].page == 36  # 20+16
    assert anchor.match_overrides[("Ch4",)].page == 46  # 30+16


def test_phase2_recalibrate_miss_drops_suffix_from_tree() -> None:
    """When suffix cannot be recalibrated, those leaves leave the TOC tree."""
    from app.services.document_agent.calibration.procedure import (
        anchor_hierarchy_from_regimes,
    )
    from app.services.document_agent.calibration.scan import TitleScanResult
    from app.services.document_agent.calibration.types import (
        CalibrationRegime,
        CalibrationResult,
        CalibrationSample,
    )

    leaves = [
        _leaf("Ch1", 1),
        _leaf("Ch2", 5),
        _leaf("Ch3", 20),
        _leaf("Ch4", 30),
    ]
    phase1 = CalibrationResult(
        status="ok",
        offset=10,
        regimes=[
            CalibrationRegime(
                kind="decimal",
                offset=10,
                offset_status="ok",
                entry_indices=[0, 1, 2, 3],
                samples=[
                    CalibrationSample(title="Ch1", physical=11)
                ],
            )
        ],
    )
    ctx = _ctx()

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        expected = int(kwargs["candidate_matches"][0].page)
        title = str(kwargs.get("title") or "")
        # Prefix Ch1/Ch2 at offset=10 confirm; Ch3/Ch4 miss under old offset.
        ok_pages = {11, 15}  # 1+10, 5+10
        if expected in ok_pages and title in {"Ch1", "Ch2"}:
            return {"selected_page": expected, "reason": "ok"}
        return {"selected_page": None, "reason": "miss"}

    def fake_scan(**kwargs: Any) -> TitleScanResult:
        return TitleScanResult(
            title=str(kwargs.get("title") or ""),
            found=False,
            found_page=None,
            scanned_pages=[],
            next_start=None,
        )

    with (
        _patch_verify(fake_verify),
        patch(
            "app.services.document_agent.calibration.scan.scan_title_forward",
            fake_scan,
        ),
    ):
        working, anchor = anchor_hierarchy_from_regimes(
            nodes=leaves,
            result=phase1,
            entries=[
                {"heading": "Ch1", "level": 1, "page_number": 1},
                {"heading": "Ch2", "level": 1, "page_number": 5},
                {"heading": "Ch3", "level": 1, "page_number": 20},
                {"heading": "Ch4", "level": 1, "page_number": 30},
            ],
            page_texts={11: "Ch1", 15: "Ch2", 30: "noise", 40: "noise"},
            body_pages=list(range(1, 50)),
            page_count=50,
            ctx=ctx,
        )

    titles = [n.title for n in working]
    assert titles == ["Ch1", "Ch2"]
    assert ("Ch1",) in anchor.match_overrides
    assert ("Ch2",) in anchor.match_overrides
    assert ("Ch3",) not in anchor.match_overrides
    assert ("Ch4",) not in anchor.match_overrides
    assert anchor.pruned_count >= 2


def _printed_page_parent(title: str, printed: int, child: TitleNode) -> TitleNode:
    return TitleNode(title=title, level=1, printed_page=printed, children=[child])


def test_parent_backfill_uses_descendant_regime_offset() -> None:
    from app.services.document_agent.structure import anchoring_primitives as primitives

    early_child = TitleNode(title="A.1", level=2, printed_page=12, children=[])
    late_child = TitleNode(title="B.1", level=2, printed_page=42, children=[])
    section_a = _printed_page_parent("Section A", 10, early_child)
    section_b = _printed_page_parent("Section B", 40, late_child)
    matches = {
        **primitives.bulk_offset_matches([(("Section A", "A.1"), early_child)], 5),
        **primitives.bulk_offset_matches([(("Section B", "B.1"), late_child)], 9),
    }

    parents = primitives.backfill_parent_offset_matches(
        nodes=[section_a, section_b],
        matches=matches,
        page_count=60,
    )

    assert parents[("Section A",)].page == 15
    assert parents[("Section B",)].page == 49
    assert parents[("Section A",)].evidence["parent_backfill"] is True


def test_parent_backfill_skips_unanchored_and_out_of_range() -> None:
    from app.services.document_agent.structure import anchoring_primitives as primitives

    tail_child = TitleNode(title="Tail.1", level=2, printed_page=96, children=[])
    ghost_child = TitleNode(title="Ghost.1", level=2, printed_page=11, children=[])
    tail = _printed_page_parent("Tail", 95, tail_child)
    ghost = _printed_page_parent("Ghost", 10, ghost_child)
    matches = primitives.bulk_offset_matches([(("Tail", "Tail.1"), tail_child)], 8)

    parents = primitives.backfill_parent_offset_matches(
        nodes=[tail, ghost],
        matches=matches,
        page_count=100,
    )

    assert parents == {}


def test_multi_regime_phase2_merges_physical_overrides() -> None:
    """Roman + decimal + prefixed each apply their own offset → physical pages."""
    from app.services.document_agent.calibration.procedure import (
        anchor_hierarchy_from_regimes,
    )
    from app.services.document_agent.calibration.types import (
        CalibrationRegime,
        CalibrationResult,
        CalibrationSample,
    )
    from app.services.document_agent.structure.hierarchy_locator import extract_toc_nodes

    toc = [
        {
            "toc_range": [1, 1],
            "toc_with_level": [
                {"heading": "Glossary", "level": 1, "page_number": "iv"},
                {"heading": "Summary", "level": 1, "page_number": 1},
                {"heading": "Risks", "level": 1, "page_number": 10},
                {"heading": "Financials", "level": 1, "page_number": "F-1"},
            ],
        }
    ]
    nodes = extract_toc_nodes(toc)
    assert nodes[0].page_kind == "roman"
    assert nodes[0].printed_page == 4
    assert nodes[1].page_kind == "decimal"
    assert nodes[3].page_kind == "prefixed"
    assert nodes[3].printed_page == 1

    phase1 = CalibrationResult(
        status="ok",
        offset=20,
        regimes=[
            CalibrationRegime(
                kind="roman",
                offset=16,
                offset_status="ok",
                entry_indices=[0],
                samples=[
                    CalibrationSample(title="Glossary", physical=20)
                ],
            ),
            CalibrationRegime(
                kind="decimal",
                offset=20,
                offset_status="ok",
                entry_indices=[1, 2],
                samples=[
                    CalibrationSample(title="Summary", physical=21)
                ],
            ),
            CalibrationRegime(
                kind="prefixed",
                offset=300,
                offset_status="ok",
                entry_indices=[3],
                samples=[
                    CalibrationSample(title="Financials", physical=301)
                ],
            ),
        ],
    )
    ctx = _ctx()

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        expected = kwargs["candidate_matches"][0].page
        return {"selected_page": expected, "reason": "ok"}

    with _patch_verify(fake_verify):
        _working, anchor = anchor_hierarchy_from_regimes(
            nodes=nodes,
            result=phase1,
            entries=toc[0]["toc_with_level"],
            page_texts={20: "Glossary", 21: "Summary", 30: "Risks", 301: "Financials"},
            body_pages=list(range(1, 320)),
            page_count=320,
            ctx=ctx,
        )

    assert anchor.match_overrides[("Glossary",)].page == 20
    assert anchor.match_overrides[("Summary",)].page == 21
    assert anchor.match_overrides[("Risks",)].page == 30
    assert anchor.match_overrides[("Financials",)].page == 301
    assert anchor.offset == 20  # primary decimal