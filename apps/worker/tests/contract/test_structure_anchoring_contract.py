"""Contract tests for structure_anchoring (Phase-2) + calibrate wiring."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.budget import BudgetTracker
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.state import AgentBlackboard
from app.services.document_agent.structure.hierarchy_locator import TitleMatch, TitleNode
from app.services.document_agent.structure import structure_anchoring as anchoring


@contextmanager
def _patch_verify(fake_verify: Callable[..., dict[str, Any]]) -> Iterator[None]:
    """Patch verify on the module dict closed over by live anchoring code."""
    from app.services.document_agent.agents.calibration import procedure

    dicts = [procedure.offset_guided_anchoring.__globals__, anchoring.__dict__]
    seen: set[int] = set()
    originals: list[tuple[dict[str, Any], Any]] = []
    for module_dict in dicts:
        dict_id = id(module_dict)
        if dict_id in seen:
            continue
        seen.add(dict_id)
        originals.append((module_dict, module_dict.get("verify_section_page_choice")))
        module_dict["verify_section_page_choice"] = fake_verify
    try:
        yield
    finally:
        for module_dict, original in originals:
            if original is None:
                module_dict.pop("verify_section_page_choice", None)
            else:
                module_dict["verify_section_page_choice"] = original


def _ctx() -> ToolContext:
    return ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="job-anchor",
        blackboard=AgentBlackboard(),
        budget=BudgetTracker(plan_budget=50_000, visual_budget=80_000),
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


def test_null_page_parent_located_via_compact_text() -> None:
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
            confidence=0.9,
            source="agent_vlm",
            matched_line="",
            score=0.9,
            candidates=[5],
            evidence={"calibration": True, "printed_page": 3},
        )
    }

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        expected = kwargs["candidate_matches"][0].page
        return {"selected_page": expected, "confidence": 0.9, "reason": "ok"}

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

    from app.services.document_agent.agents.calibration.types import (
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
                    CalibrationSample(
                        title="Only", printed_label=2, physical=4
                    )
                ],
            )
        ],
    )

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        expected = kwargs["candidate_matches"][0].page
        return {"selected_page": expected, "confidence": 0.95, "reason": "ok"}

    with (
        patch(
            "app.services.document_agent.agents.calibration.service.calibrate_offset",
            return_value=phase1,
        ),
        _patch_verify(fake_verify),
    ):
        nodes, anchor = anchoring.anchor_hierarchy(
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


def test_phase2_recalibrate_miss_drops_suffix_from_tree() -> None:
    """When suffix cannot be recalibrated, those leaves leave the TOC tree."""
    from app.services.document_agent.agents.calibration.procedure import (
        anchor_hierarchy_from_regimes,
    )
    from app.services.document_agent.agents.calibration.types import (
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
                    CalibrationSample(title="Ch1", printed_label=1, physical=11)
                ],
            )
        ],
    )
    ctx = _ctx()

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        expected = int(kwargs["candidate_matches"][0].page)
        title = str(kwargs.get("title") or "")
        # Prefix Ch1/Ch2 at offset=10 confirm; Ch3/Ch4 and recalibrate (+1..+5) miss.
        ok_pages = {11, 15}  # 1+10, 5+10
        if expected in ok_pages and title in {"Ch1", "Ch2"}:
            return {"selected_page": expected, "confidence": 0.95, "reason": "ok"}
        # Tail / bisect mid / recalibrate probes for Ch3/Ch4 all fail.
        return {"selected_page": None, "confidence": 0.1, "reason": "miss"}

    with _patch_verify(fake_verify):
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


def test_multi_regime_phase2_merges_physical_overrides() -> None:
    """Roman + decimal + prefixed each apply their own offset → physical pages."""
    from app.services.document_agent.agents.calibration.procedure import (
        anchor_hierarchy_from_regimes,
    )
    from app.services.document_agent.agents.calibration.types import (
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
                    CalibrationSample(title="Glossary", printed_label="iv", physical=20)
                ],
            ),
            CalibrationRegime(
                kind="decimal",
                offset=20,
                offset_status="ok",
                entry_indices=[1, 2],
                samples=[
                    CalibrationSample(title="Summary", printed_label=1, physical=21)
                ],
            ),
            CalibrationRegime(
                kind="prefixed",
                offset=300,
                offset_status="ok",
                entry_indices=[3],
                samples=[
                    CalibrationSample(
                        title="Financials", printed_label="F-1", physical=301
                    )
                ],
            ),
        ],
    )
    ctx = _ctx()

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        expected = kwargs["candidate_matches"][0].page
        return {"selected_page": expected, "confidence": 0.95, "reason": "ok"}

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