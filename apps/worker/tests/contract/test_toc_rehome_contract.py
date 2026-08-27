"""Contract tests for same-forest TOC paged-leaf rehome. Synthetic trees only."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.manifest import TocResult, ToolContext
from app.services.document_agent.state import ProfileBlackboard
from app.services.document_agent.structure.anchoring_primitives import (
    SkeletonAnchor,
)
from app.services.document_agent.structure.hierarchy_locator import (
    TitleMatch,
    TitleNode,
    resolve_hierarchy_page_ranges,
)
from app.services.document_agent.structure.toc_anchoring import run_toc_anchoring
from app.services.document_agent.structure.toc_rehome import rehome_forest


def _match(title: str, page: int) -> TitleMatch:
    return TitleMatch(
        page=page,
        source="anchored",
        matched_line=title,
        candidates=[page],
        evidence={},
    )


def _overrides(pages: dict[tuple[str, ...], int]) -> dict[tuple[str, ...], TitleMatch]:
    return {path: _match(path[-1], page) for path, page in pages.items()}


def _titles(nodes: list[TitleNode]) -> list[str]:
    return [node.title for node in nodes]


def test_same_parent_backjump_inserts_after_nearest_prev_leaf() -> None:
    nodes = [
        TitleNode(title="A", level=1, printed_page=10),
        TitleNode(title="B", level=1, printed_page=20),
        TitleNode(title="C", level=1, printed_page=15),
    ]
    result = rehome_forest(
        nodes,
        _overrides({("A",): 10, ("B",): 20, ("C",): 15}),
    )
    # C@15 nearest prev leaf A@10 → after A under root.
    assert _titles(result.nodes) == ["A", "C", "B"]
    assert result.match_overrides[("C",)].page == 15
    assert not result.nodes[1].children


def test_no_prev_leaf_by_page_is_noop_for_that_backjump() -> None:
    """Backjump with no TOC-earlier leaf page<=P is not moved (no fallback)."""
    nodes = [
        TitleNode(title="A", level=1, printed_page=10),
        TitleNode(title="B", level=1, printed_page=20),
        TitleNode(title="C", level=1, printed_page=5),
    ]
    result = rehome_forest(
        nodes,
        _overrides({("A",): 10, ("B",): 20, ("C",): 5}),
    )
    assert _titles(result.nodes) == ["A", "B", "C"]
    assert result.events == []


def test_equal_page_backjump_inserts_after_front_same_page_leaves() -> None:
    """Rehome @116 with one or more front @116 leaves → after that same-page group."""
    nodes = [
        TitleNode(title="A116", level=1, printed_page=116),
        TitleNode(title="B116", level=1, printed_page=116),
        TitleNode(title="Late", level=1, printed_page=200),
        TitleNode(title="C116", level=1, printed_page=116),
    ]
    result = rehome_forest(
        nodes,
        _overrides(
            {
                ("A116",): 116,
                ("B116",): 116,
                ("Late",): 200,
                ("C116",): 116,
            }
        ),
    )
    assert _titles(result.nodes) == ["A116", "B116", "C116", "Late"]
    assert result.events[0]["anchor_path"] == ["B116"]


def test_unpaged_shell_leaf_inserts_after_nearest_prev_and_drops_shell() -> None:
    nodes = [
        TitleNode(title="Preamble", level=1, printed_page=6),
        TitleNode(title="References", level=1, printed_page=61),
        TitleNode(
            title="List of tables",
            level=1,
            children=[
                TitleNode(title="Table 1", level=2, printed_page=7),
                TitleNode(title="Table 2", level=2, printed_page=9),
            ],
        ),
    ]
    result = rehome_forest(
        nodes,
        _overrides(
            {
                ("Preamble",): 6,
                ("References",): 61,
                ("List of tables", "Table 1"): 7,
                ("List of tables", "Table 2"): 9,
            }
        ),
    )
    assert "List of tables" not in _titles(result.nodes)
    # Table1 after Preamble@6; Table2 after Table1@7 — all stay leaves.
    assert _titles(result.nodes) == ["Preamble", "Table 1", "Table 2", "References"]
    assert not result.nodes[1].children
    assert not result.nodes[2].children
    assert result.match_overrides[("Table 1",)].page == 7
    assert result.match_overrides[("Table 2",)].page == 9


def test_equal_page_points_at_same_first_segment_group() -> None:
    """Both @117 leaves anchor only into the first monotonic segment."""
    nodes = [
        TitleNode(title="Prev", level=1, printed_page=116),
        TitleNode(title="Late", level=1, printed_page=200),
        TitleNode(
            title="Shell",
            level=1,
            children=[
                TitleNode(title="T1", level=2, printed_page=117),
                TitleNode(title="T2", level=2, printed_page=117),
            ],
        ),
    ]
    result = rehome_forest(
        nodes,
        _overrides(
            {
                ("Prev",): 116,
                ("Late",): 200,
                ("Shell", "T1"): 117,
                ("Shell", "T2"): 117,
            }
        ),
    )
    assert "Shell" not in _titles(result.nodes)
    assert _titles(result.nodes) == ["Prev", "T1", "T2", "Late"]
    assert [event["anchor_path"] for event in result.events] == [
        ["Prev"],
        ["Prev"],
    ]


def test_monotonic_forest_is_noop() -> None:
    nodes = [
        TitleNode(title="A", level=1, printed_page=8),
        TitleNode(title="B", level=1, printed_page=14),
        TitleNode(title="C", level=1, printed_page=20),
    ]
    overrides = _overrides({("A",): 8, ("B",): 14, ("C",): 20})
    result = rehome_forest(nodes, overrides)
    assert _titles(result.nodes) == ["A", "B", "C"]
    assert result.match_overrides == overrides
    assert result.events == []


def test_every_leaf_in_post_break_segment_attempts_first_segment() -> None:
    nodes = [
        TitleNode(
            title="Chapter",
            level=1,
            printed_page=10,
            children=[
                TitleNode(title="Intro", level=2, printed_page=11),
                TitleNode(title="Late", level=2, printed_page=30),
                TitleNode(title="Early", level=2, printed_page=12),
            ],
        ),
        TitleNode(title="Next", level=1, printed_page=40),
    ]
    result = rehome_forest(
        nodes,
        _overrides(
            {
                ("Chapter",): 10,
                ("Chapter", "Intro"): 11,
                ("Chapter", "Late"): 30,
                ("Chapter", "Early"): 12,
                ("Next",): 40,
            }
        ),
    )
    assert _titles(result.nodes) == ["Chapter"]
    assert _titles(result.nodes[0].children) == ["Intro", "Early", "Late", "Next"]
    assert ("Chapter", "Early") in result.match_overrides
    assert ("Chapter", "Next") in result.match_overrides


def test_each_later_segment_anchors_only_to_first_segment() -> None:
    nodes = [
        TitleNode(title="A", level=1, printed_page=10),
        TitleNode(title="B", level=1, printed_page=20),
        TitleNode(
            title="Shell 1",
            level=1,
            children=[
                TitleNode(title="C", level=2, printed_page=15),
                TitleNode(title="D", level=2, printed_page=17),
            ],
        ),
        TitleNode(
            title="Shell 2",
            level=1,
            children=[
                TitleNode(title="E", level=2, printed_page=12),
                TitleNode(title="F", level=2, printed_page=16),
            ],
        ),
    ]
    result = rehome_forest(
        nodes,
        _overrides(
            {
                ("A",): 10,
                ("B",): 20,
                ("Shell 1", "C"): 15,
                ("Shell 1", "D"): 17,
                ("Shell 2", "E"): 12,
                ("Shell 2", "F"): 16,
            }
        ),
    )

    assert "Shell 1" not in _titles(result.nodes)
    assert "Shell 2" not in _titles(result.nodes)
    assert _titles(result.nodes) == ["A", "E", "C", "F", "D", "B"]
    event_by_title = {
        event["new_path"][-1]: event for event in result.events
    }
    assert event_by_title["C"]["anchor_path"] == ["A"]
    assert event_by_title["D"]["anchor_path"] == ["A"]
    assert event_by_title["E"]["anchor_path"] == ["A"]
    assert event_by_title["F"]["anchor_path"] == ["A"]


def test_prune_duplicate_when_same_path_and_same_page_as_first_segment() -> None:
    nodes = [
        TitleNode(title="Abbreviations", level=1, printed_page=4),
        TitleNode(title="Body", level=1, printed_page=10),
        TitleNode(title="Late", level=1, printed_page=50),
        TitleNode(title="Abbreviations", level=1, printed_page=4),
    ]
    overrides = _overrides(
        {
            ("Abbreviations",): 4,
            ("Body",): 10,
            ("Late",): 50,
        }
    )
    result = rehome_forest(nodes, overrides)
    assert _titles(result.nodes) == ["Abbreviations", "Body", "Late"]
    assert len(result.events) == 1
    assert result.events[0]["action"] == "pruned"
    assert result.events[0]["source_path"] == ["Abbreviations"]
    assert result.match_overrides[("Abbreviations",)].page == 4
    assert "toc_rehome" not in (result.match_overrides[("Abbreviations",)].evidence or {})


def test_prune_same_path_page_even_when_page_is_toc_page() -> None:
    """same_path_page is judged before toc_page skip and still prunes."""
    nodes = [
        TitleNode(title="Abbreviations", level=1, printed_page=4),
        TitleNode(title="Body", level=1, printed_page=10),
        TitleNode(title="Late", level=1, printed_page=50),
        TitleNode(title="Abbreviations", level=1, printed_page=4),
    ]
    result = rehome_forest(
        nodes,
        _overrides(
            {
                ("Abbreviations",): 4,
                ("Body",): 10,
                ("Late",): 50,
            }
        ),
        toc_pages=[4],
    )
    assert _titles(result.nodes) == ["Abbreviations", "Body", "Late"]
    assert result.events[0]["action"] == "pruned"
    assert "toc_rehome" not in (result.match_overrides[("Abbreviations",)].evidence or {})


def test_skip_rehome_when_physical_page_is_toc_page() -> None:
    nodes = [
        TitleNode(title="Lead", level=1, printed_page=5),
        TitleNode(title="Body", level=1, printed_page=20),
        TitleNode(
            title="Shell",
            level=1,
            children=[TitleNode(title="Early", level=2, printed_page=3)],
        ),
    ]
    result = rehome_forest(
        nodes,
        _overrides(
            {
                ("Lead",): 5,
                ("Body",): 20,
                ("Shell", "Early"): 3,
            }
        ),
        toc_pages=[2, 3, 4],
    )
    assert result.events == []
    assert "Shell" in _titles(result.nodes)
    assert _titles(result.nodes[2].children) == ["Early"]
    assert "toc_rehome" not in (
        result.match_overrides[("Shell", "Early")].evidence or {}
    )


def test_rehome_leaves_attach_to_resolved_scope_without_cutting_boundaries() -> None:
    nodes = [
        TitleNode(title="Lead", level=1, printed_page=21),
        TitleNode(
            title="Chapter 7",
            level=1,
            printed_page=22,
            children=[
                TitleNode(title="7.1", level=2, printed_page=24),
                TitleNode(title="7.2", level=2, printed_page=26),
            ],
        ),
        TitleNode(title="Recommendation", level=1, printed_page=22),
        TitleNode(title="Table 7", level=1, printed_page=23),
        TitleNode(title="Chapter 8", level=1, printed_page=27),
    ]
    overrides = _overrides(
        {
            ("Lead",): 21,
            ("Chapter 7",): 22,
            ("Chapter 7", "7.1"): 24,
            ("Chapter 7", "7.2"): 26,
            ("Recommendation",): 22,
            ("Table 7",): 23,
            ("Chapter 8",): 27,
        }
    )
    for path in (("Recommendation",), ("Table 7",)):
        overrides[path] = TitleMatch(
            page=overrides[path].page,
            source=overrides[path].source,
            matched_line=overrides[path].matched_line,
            candidates=overrides[path].candidates,
            evidence={"toc_rehome": {"segment_index": 1}},
        )

    ranges = resolve_hierarchy_page_ranges(
        nodes,
        page_count=30,
        match_overrides=overrides,
    )
    by_title = {item.title: item for item in ranges}

    assert by_title["7.1"].match is not None
    assert by_title["7.1"].start_page == 24
    assert by_title["7.1"].evidence["source"] == "anchored"
    assert by_title["7.2"].start_page == 26
    assert (by_title["Recommendation"].start_page, by_title["Recommendation"].end_page) == (
        22,
        24,
    )
    assert (by_title["Table 7"].start_page, by_title["Table 7"].end_page) == (
        22,
        24,
    )
    assert by_title["Recommendation"].evidence["status"] == "rehome_attached"
    assert by_title["Table 7"].evidence["status"] == "rehome_attached"


def test_rehome_leaf_attaches_by_physical_page_after_structural_resolve() -> None:
    nodes = [
        TitleNode(
            title="Chapter 3",
            level=1,
            printed_page=7,
            children=[
                TitleNode(title="3.1", level=2, printed_page=7),
                TitleNode(title="3.2", level=2, printed_page=8),
                TitleNode(title="Table 3", level=2, printed_page=9),
            ],
        ),
        TitleNode(title="Chapter 4", level=1, printed_page=8),
        TitleNode(title="Chapter 5", level=1, printed_page=12),
    ]
    overrides = _overrides(
        {
            ("Chapter 3",): 7,
            ("Chapter 3", "3.1"): 7,
            ("Chapter 3", "3.2"): 8,
            ("Chapter 3", "Table 3"): 9,
            ("Chapter 4",): 8,
            ("Chapter 5",): 12,
        }
    )
    table_path = ("Chapter 3", "Table 3")
    table_match = overrides[table_path]
    overrides[table_path] = TitleMatch(
        page=table_match.page,
        source=table_match.source,
        matched_line=table_match.matched_line,
        candidates=table_match.candidates,
        evidence={"toc_rehome": {"segment_index": 1}},
    )

    ranges = resolve_hierarchy_page_ranges(
        nodes,
        page_count=15,
        match_overrides=overrides,
    )
    by_title = {item.title: item for item in ranges}

    assert (by_title["3.2"].start_page, by_title["3.2"].end_page) == (8, 8)
    assert (by_title["Table 3"].start_page, by_title["Table 3"].end_page) == (
        8,
        12,
    )
    assert by_title["Table 3"].evidence["scope_host_path"] == ["Chapter 4"]
    assert [item.title for item in ranges] == [
        "3.1",
        "3.2",
        "Table 3",
        "Chapter 4",
        "Chapter 5",
    ]


def test_nested_leaf_inserts_after_nearest_root_leaf() -> None:
    nodes = [
        TitleNode(title="Chapter", level=1, printed_page=10),
        TitleNode(
            title="List",
            level=1,
            children=[
                TitleNode(
                    title="FrontMatter",
                    level=2,
                    printed_page=50,
                    children=[
                        TitleNode(title="Late", level=3, printed_page=80),
                        TitleNode(title="Early", level=3, printed_page=55),
                    ],
                ),
            ],
        ),
    ]
    result = rehome_forest(
        nodes,
        _overrides(
            {
                ("Chapter",): 10,
                ("List", "FrontMatter"): 50,
                ("List", "FrontMatter", "Late"): 80,
                ("List", "FrontMatter", "Early"): 55,
            }
        ),
    )
    # Early@55 after Chapter@10 at root; List (parent shell with Late) stays.
    assert _titles(result.nodes) == ["Chapter", "List", "Early"]
    assert not result.nodes[2].children
    front = result.nodes[1].children[0]
    assert front.title == "FrontMatter"
    assert _titles(front.children) == ["Late"]
    assert result.match_overrides[("Early",)].page == 55


def test_non_leaf_not_rehomed_even_if_page_looks_early() -> None:
    nodes = [
        TitleNode(title="Chapter", level=1, printed_page=10),
        TitleNode(
            title="List",
            level=1,
            children=[
                TitleNode(
                    title="FrontMatter",
                    level=2,
                    printed_page=5,
                    children=[
                        TitleNode(title="Child", level=3, printed_page=50),
                    ],
                ),
            ],
        ),
    ]
    result = rehome_forest(
        nodes,
        _overrides(
            {
                ("Chapter",): 10,
                ("List", "FrontMatter"): 5,
                ("List", "FrontMatter", "Child"): 50,
            }
        ),
    )
    assert _titles(result.nodes) == ["Chapter", "List"]
    assert result.nodes[1].children[0].title == "FrontMatter"
    assert result.events == []


def _ctx(*, hierarchies: list[dict], page_count: int = 80) -> ToolContext:
    ctx = ToolContext(
        pdf_path="/tmp/rehome.pdf",
        job_id="rehome-test",
        blackboard=ProfileBlackboard(page_count=page_count),
        trace=None,
        settings={},
    )
    ctx.blackboard.toc_hierarchies = hierarchies
    ctx.blackboard.toc_result = TocResult(method="vlm_batch", toc_pages=[1])
    ctx.blackboard.page_full_text_cache = {
        page: "body" for page in range(1, page_count + 1)
    }
    return ctx


def test_run_toc_anchoring_single_toc_still_runs_rehome() -> None:
    hierarchies = [
        {
            "toc_range": [1, 1],
            "toc_range_unit": "page",
            "toc_with_level": [
                {"heading": "Alpha", "level": 1, "page_number": 10},
                {"heading": "Bravo", "level": 1, "page_number": 20},
                {"heading": "Charlie", "level": 1, "page_number": 15},
            ],
        }
    ]
    ctx = _ctx(hierarchies=hierarchies, page_count=30)

    def _fake_anchor(**kwargs):
        nodes = [
            TitleNode(title="Alpha", level=1, printed_page=10),
            TitleNode(title="Bravo", level=1, printed_page=20),
            TitleNode(title="Charlie", level=1, printed_page=15),
        ]
        anchor = SkeletonAnchor(
            offset=0,
            offset_status="ok",
            match_overrides=_overrides(
                {("Alpha",): 10, ("Bravo",): 20, ("Charlie",): 15}
            ),
            null_page_report=[],
            bulk_count=3,
        )
        return nodes, anchor

    with patch(
        "app.services.document_agent.calibration.orchestrator.anchor_hierarchy",
        side_effect=_fake_anchor,
    ):
        run_toc_anchoring(ctx)

    assert ctx.blackboard.skeleton_anchor is not None
    assert ctx.blackboard.skeleton_nodes is not None
    assert ctx.blackboard.pending_skeleton_anchors == []
    assert [node["title"] for node in ctx.blackboard.skeleton_nodes] == [
        "Alpha",
        "Charlie",
        "Bravo",
    ]


def test_run_toc_anchoring_rehome_before_classify_keeps_contained_graft() -> None:
    """Pending calibrate must succeed without opening pdf_path; graft after rehome.

    Stub the calibration *internals* (same pattern as toc_graft contracts), not
    ``_calibrate_pending_tocs`` itself. Patching that helper by string path is
    brittle across the full contract suite and lets the real pending path try to
    render ``pdf_path`` (``/tmp/rehome.pdf``), then drop the pending record.
    """
    hierarchies = [
        {
            "toc_range": [1, 1],
            "toc_range_unit": "page",
            "toc_with_level": [
                {"heading": "Host", "level": 1, "page_number": 10},
                {"heading": "Tail", "level": 1, "page_number": 40},
            ],
        },
        {
            "toc_range": [5, 5],
            "toc_range_unit": "page",
            "toc_with_level": [
                {"heading": "Inner", "level": 1, "page_number": 12},
            ],
        },
    ]
    ctx = _ctx(hierarchies=hierarchies, page_count=50)

    def _fake_anchor(**kwargs):
        nodes = [
            TitleNode(title="Host", level=1, printed_page=10),
            TitleNode(title="Tail", level=1, printed_page=40),
        ]
        anchor = SkeletonAnchor(
            offset=0,
            offset_status="ok",
            match_overrides=_overrides({("Host",): 10, ("Tail",): 40}),
            null_page_report=[],
            bulk_count=2,
        )
        return nodes, anchor

    inner = TitleNode(title="Inner", level=1, printed_page=12)
    inner_anchor = SkeletonAnchor(
        offset=0,
        offset_status="ok",
        match_overrides=_overrides({("Inner",): 12}),
        null_page_report=[],
        bulk_count=1,
    )

    with (
        patch(
            "app.services.document_agent.calibration.orchestrator.anchor_hierarchy",
            side_effect=_fake_anchor,
        ),
        patch(
            "app.services.document_agent.calibration.service.calibrate_offset",
            return_value=object(),
        ),
        patch(
            "app.services.document_agent.calibration.procedure.pick_primary_offset",
            return_value=0,
        ),
        patch(
            "app.services.document_agent.calibration.procedure.finalize_calibration_result",
            return_value=([inner], inner_anchor, True),
        ),
    ):
        run_toc_anchoring(ctx)

    record = ctx.blackboard.pending_skeleton_anchors[0]
    assert record["relationship"] == "contained"
    assert record.get("grafted") is True
    host = next(
        node for node in ctx.blackboard.skeleton_nodes if node["title"] == "Host"
    )
    assert any(child["title"] == "Inner" for child in host["children"])
