"""Contract tests for structure_anchoring (moved from skeleton_extractor)."""

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

from app.services.document_agent.budget import BudgetTracker
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.state import AgentBlackboard
from app.services.document_agent.structure.hierarchy_locator import TitleMatch, TitleNode
from app.services.document_agent.structure import structure_anchoring as anchoring


def _ctx() -> ToolContext:
    return ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="job-anchor",
        blackboard=AgentBlackboard(),
        budget=BudgetTracker(plan_budget=50_000, visual_budget=80_000),
        trace=None,
        settings={},
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


def test_calibrate_and_bulk_via_mocked_offset() -> None:
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

    with (
        patch.object(
            anchoring,
            "calibrate_offset",
            return_value=(2, seed),
        ),
        patch.object(
            anchoring,
            "verify_section_page_choice",
            side_effect=fake_verify,
        ),
    ):
        offset, seed_overrides = anchoring.calibrate_offset(
            nodes=leaves,
            toc_hierarchies=[{"toc_range": [1, 2], "toc_tree": {}}],
            ctx=ctx,
            page_texts={},
            page_count=30,
        )
        assert offset == 2
        assert seed_overrides
        matches = anchoring.offset_guided_anchoring(
            nodes=leaves,
            offset=offset,
            ctx=ctx,
            page_count=30,
            calibration_overrides=seed_overrides,
        )
    assert matches is not None
    assert len(matches) >= 3
    assert matches[("Intro",)].page == 5
    assert matches[("Body",)].page == 12
    assert matches[("End",)].page == 22


def test_anchor_hierarchy_returns_skeleton_anchor_fields() -> None:
    leaves = [_leaf("Only", 2)]
    toc_hierarchies = [{"toc_range": [1, 1], "toc_tree": {}}]
    ctx = _ctx()
    seed = {
        ("Only",): TitleMatch(
            page=4,
            confidence=0.95,
            source="agent_vlm",
            matched_line="",
            score=0.95,
            candidates=[4],
            evidence={"calibration": True},
        )
    }

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        expected = kwargs["candidate_matches"][0].page
        return {"selected_page": expected, "confidence": 0.95, "reason": "ok"}

    with (
        patch.object(anchoring, "calibrate_offset", return_value=(2, seed)),
        patch.object(
            anchoring,
            "verify_section_page_choice",
            side_effect=fake_verify,
        ),
    ):
        nodes, anchor = anchoring.anchor_hierarchy(
            nodes=leaves,
            toc_hierarchies=toc_hierarchies,
            page_texts={4: "Only\ntext"},
            body_pages=[2, 3, 4, 5],
            page_count=10,
            ctx=ctx,
        )
    assert isinstance(anchor, anchoring.SkeletonAnchor)
    assert anchor.offset == 2
    assert anchor.offset_status == "ok"
    assert isinstance(anchor.match_overrides, dict)
    assert isinstance(anchor.null_page_report, list)
    assert isinstance(anchor.bulk_count, int)
    assert isinstance(anchor.pruned_count, int)
    assert nodes
