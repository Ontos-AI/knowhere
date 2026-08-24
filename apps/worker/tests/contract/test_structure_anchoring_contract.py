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


def test_all_null_page_nodes_unresolved_without_ctx_in_parent_first_order() -> None:
    from app.services.document_agent.structure.null_page_react import (
        locate_null_page_overrides,
    )

    parent = TitleNode(
        title="Chapter",
        level=1,
        printed_page=None,
        children=[TitleNode(title="Orphan", level=2, printed_page=None, children=[])],
    )
    overrides, report = locate_null_page_overrides(
        nodes=[parent],
        match_overrides={},
        body_pages=[1, 2, 3],
        ctx=None,
    )
    assert overrides == {}
    assert [row["path_titles"] for row in report] == [
        ["Chapter"],
        ["Chapter", "Orphan"],
    ]
    assert [row["kind"] for row in report] == ["parent", "leaf"]
    assert {row["result"] for row in report} == {"unresolved_no_ctx"}


def test_null_page_leaf_kept_by_pre_react_prune() -> None:
    """Printed unanchored leaves drop; null-page leaves survive until ReAct."""
    nodes = [
        _leaf("PrintedOk", 1),
        _leaf("PrintedMiss", 10),
        TitleNode(title="NullLeaf", level=1, printed_page=None, children=[]),
    ]
    overrides = anchoring.bulk_offset_matches(
        [(("PrintedOk",), nodes[0])],
        offset=0,
    )
    kept, removed = anchoring.prune_unanchored_toc_leaves(
        nodes,
        match_overrides=overrides,
        keep_null_page_nodes=True,
    )
    assert removed == 1
    assert [n.title for n in kept] == ["PrintedOk", "NullLeaf"]


def test_null_page_react_continues_later_siblings_after_failure() -> None:
    from app.services.document_agent.structure import null_page_react as npr
    from app.services.document_agent.structure.null_page_react import (
        locate_null_page_overrides,
    )

    nodes = [
        TitleNode(title="A", level=1, printed_page=None, children=[]),
        TitleNode(title="B", level=1, printed_page=None, children=[]),
    ]
    ctx = _ctx()

    def probe(**kwargs: Any) -> tuple[TitleMatch | None, list[dict[str, Any]], int, str]:
        if kwargs["title"] == "A":
            return None, [], 0, "react_give_up"
        return (
            TitleMatch(
                page=4,
                source="react_line_grep_vlm",
                matched_line="B",
                candidates=[4],
                evidence={"accept": "react_line_grep_vlm"},
            ),
            [],
            1,
            "react_line_grep_vlm",
        )

    with patch.object(npr, "_locate_with_react", side_effect=probe):
        overrides, report = locate_null_page_overrides(
            nodes=nodes,
            match_overrides={},
            body_pages=[1, 2, 3, 4, 5],
            ctx=ctx,
        )

    assert overrides[("B",)].page == 4
    assert report[0]["result"] == "react_give_up"
    assert report[1]["result"] == "react_line_grep_vlm"


def test_null_page_react_parent_first_walk_survives_sibling_failure() -> None:
    from app.services.document_agent.structure import null_page_react as npr
    from app.services.document_agent.structure.null_page_react import (
        locate_null_page_overrides,
    )

    nodes = [
        TitleNode(title="A", level=1, printed_page=None, children=[]),
        TitleNode(
            title="B",
            level=1,
            printed_page=None,
            children=[
                TitleNode(title="B1", level=2, printed_page=None, children=[]),
                TitleNode(title="B2", level=2, printed_page=None, children=[]),
            ],
        ),
        TitleNode(title="C", level=1, printed_page=None, children=[]),
    ]
    ctx = _ctx()
    probed: list[str] = []

    def probe(**kwargs: Any) -> tuple[TitleMatch | None, list[dict[str, Any]], int, str]:
        title = str(kwargs["title"])
        probed.append(title)
        if title == "A":
            return None, [], 0, "react_give_up"
        page = {"B": 10, "B1": 10, "B2": 11, "C": 15}[title]
        return (
            TitleMatch(
                page=page,
                source="react_line_grep_vlm",
                matched_line=title,
                candidates=[page],
                evidence={"accept": "react_line_grep_vlm"},
            ),
            [],
            1,
            "react_line_grep_vlm",
        )

    with patch.object(npr, "_locate_with_react", side_effect=probe):
        overrides, report = locate_null_page_overrides(
            nodes=nodes,
            match_overrides={},
            body_pages=list(range(1, 21)),
            ctx=ctx,
        )

    assert probed == ["A", "B", "B1", "B2", "C"]
    assert overrides[("B",)].page == 10
    assert overrides[("B", "B1")].page == 10
    assert overrides[("B", "B2")].page == 11
    assert overrides[("C",)].page == 15
    by_path = {tuple(row["path_titles"]): row for row in report}
    assert by_path[("A",)]["result"] == "react_give_up"
    assert by_path[("B", "B1")]["page"] == 10
    assert by_path[("B", "B2")]["page"] == 11
    assert by_path[("C",)]["page"] == 15


def test_null_page_react_probes_parent_before_child() -> None:
    from app.services.document_agent.structure import null_page_react as npr
    from app.services.document_agent.structure.null_page_react import (
        locate_null_page_overrides,
    )

    parent = TitleNode(
        title="Parent",
        level=1,
        printed_page=None,
        children=[
            TitleNode(title="Child", level=2, printed_page=None, children=[]),
        ],
    )
    ctx = _ctx()

    probed: list[str] = []

    def fail_probe(**kwargs: Any) -> tuple[TitleMatch | None, list[dict[str, Any]], int, str]:
        probed.append(str(kwargs["title"]))
        return None, [], 0, "react_give_up"

    with patch.object(npr, "_locate_with_react", side_effect=fail_probe):
        overrides, report = locate_null_page_overrides(
            nodes=[parent],
            match_overrides={},
            body_pages=[1, 2, 3, 4, 5],
            ctx=ctx,
        )

    assert overrides == {}
    assert probed == ["Parent", "Child"]
    assert [row["path_titles"] for row in report] == [
        ["Parent"],
        ["Parent", "Child"],
    ]


def test_null_page_react_preserves_structural_parent_kind_after_prune() -> None:
    from app.services.document_agent.structure import null_page_react as npr
    from app.services.document_agent.structure.null_page_react import (
        locate_null_page_overrides,
    )

    # After prune(keep_null=True): former parent survives as empty shell.
    shell = TitleNode(title="Appendix", level=1, printed_page=None, children=[])
    leaf = TitleNode(title="RealLeaf", level=1, printed_page=None, children=[])
    ctx = _ctx()
    probed: list[str] = []

    def track(**kwargs: Any) -> tuple[TitleMatch | None, list[dict[str, Any]], int, str]:
        probed.append(str(kwargs["title"]))
        return None, [], 0, "react_give_up"

    with patch.object(npr, "_locate_with_react", side_effect=track):
        overrides, report = locate_null_page_overrides(
            nodes=[shell, leaf],
            match_overrides={},
            body_pages=[1, 2, 3, 4, 5],
            ctx=ctx,
            structural_parent_paths={("Appendix",)},
        )

    assert overrides == {}
    assert probed == ["Appendix", "RealLeaf"]
    assert [row["kind"] for row in report] == ["parent", "leaf"]


def test_anchor_offset_collects_structural_parent_paths_before_prune() -> None:
    """H1 production wiring: paths collected before keep_null prune."""
    parent = TitleNode(
        title="P",
        level=1,
        printed_page=None,
        children=[
            TitleNode(title="PrintedMiss", level=2, printed_page=10, children=[]),
        ],
    )
    captured: dict[str, Any] = {}

    def fake_leaf(**kwargs: Any) -> tuple[dict, list]:
        captured["structural_parent_paths"] = set(
            kwargs.get("structural_parent_paths") or set()
        )
        captured["nodes"] = kwargs["nodes"]
        return dict(kwargs["match_overrides"]), []

    with patch.object(
        anchoring, "locate_null_page_overrides", side_effect=fake_leaf
    ):
        unused_working, _anchor = anchoring.anchor_hierarchy_from_offset(
            nodes=[parent],
            offset_hint=None,
            page_texts={1: "x"},
            body_pages=[1, 2, 3],
            page_count=3,
            ctx=_ctx(),
        )

    assert ("P",) in captured["structural_parent_paths"]
    # PrintedMiss dropped by keep-null prune; structural kind remains available.
    assert len(captured["nodes"]) == 1
    assert captured["nodes"][0].title == "P"
    assert captured["nodes"][0].children == []

def test_null_page_whole_line_grep_excludes_pages_outside_body() -> None:
    """H2: grep page map is body_pages ∩ [left, right] (TOC pages dropped)."""
    from app.services.document_agent.pdf_text import PageTextBands
    from app.services.document_agent.structure.null_page_react import _whole_line_grep

    ctx = _ctx()
    ctx.blackboard.page_count = 10
    # Page 1 is TOC-excluded; title only there would inflate hits without filter.
    ctx.blackboard.page_full_text_cache = {
        1: PageTextBands(content="Appendix F Overview\nAppendix F Overview"),
        2: PageTextBands(content="noise"),
        5: PageTextBands(content="Appendix F Overview"),
    }
    status, needle, hit_pages, line_count = _whole_line_grep(
        ctx=ctx,
        query="Appendix F Overview",
        left=1,
        right=10,
        body_pages=[2, 3, 4, 5, 6],
    )
    assert status == "ok"
    assert needle
    assert hit_pages == [5]
    assert line_count == 1


def test_null_page_react_planner_history_omits_runtime_audit_fields() -> None:
    from app.services.document_agent.structure.null_page_react import _react_history_item

    history = _react_history_item(
        {
            "action": "grep",
            "query": "Furniture Item Specific Guidelines",
            "normalized_query": "furniture item specific guidelines",
            "hit_page_count": 1,
            "line_match_count": 2,
            "observation": "visual_rejected",
            "visual_selected_page": None,
            "visual_pages_checked": [304],
            "post_strip": "header",
            "seed_full_title": True,
        }
    )
    assert history == {
        "action": "grep",
        "query": "Furniture Item Specific Guidelines",
        "normalized_query": "furniture item specific guidelines",
        "hit_page_count": 1,
        "observation": "visual_rejected",
    }


def test_null_page_react_clears_page_text_search_view() -> None:
    """H3: locate always clears strip view in finally (no cross-node leak)."""
    from app.services.document_agent.structure import null_page_react as npr
    from app.services.document_agent.structure.null_page_react import _locate_with_react

    ctx = _ctx()
    ctx.blackboard.page_count = 5
    ctx.blackboard.page_full_text_cache = {page: "X" for page in range(1, 6)}
    ctx.blackboard.page_text_search_view = {1: "stale-before"}

    def dirty_grep(**kwargs: Any) -> tuple[str, str, list[int], int]:
        ctx.blackboard.page_text_search_view = {1: "dirty-during"}
        return "ok", "x", [], 0

    with (
        patch.object(npr, "_whole_line_grep", side_effect=dirty_grep),
        patch.object(
            npr,
            "_propose_react_query",
            return_value=({"action": "give_up", "query": ""}, {}),
        ),
    ):
        match, _attempts, _visual, result = _locate_with_react(
            path_titles=("T",),
            title="T",
            left=1,
            right=5,
            body_pages=[1, 2, 3, 4, 5],
            ctx=ctx,
        )

    assert match is None
    assert result == "react_give_up"
    assert ctx.blackboard.page_text_search_view is None


def test_null_page_react_progresses_windows_until_candidate_is_confirmed() -> None:
    from app.services.document_agent.structure import null_page_react as npr
    from app.services.document_agent.structure.null_page_react import _locate_with_react

    ctx = _ctx()
    verified_batches: list[list[int]] = []

    def fake_grep(**kwargs: Any) -> tuple[str, str, list[int], int]:
        return "ok", "appendix a", [260, 262, 280], 3

    def fake_verify(**kwargs: Any) -> tuple[str, int | None, str, int]:
        pages = list(kwargs["pages"])
        verified_batches.append(pages)
        selected = 262 if 262 in pages else None
        return "ok", selected, "checked", 0

    with (
        patch.object(npr, "_whole_line_grep", side_effect=fake_grep),
        patch.object(npr, "_verify_section_beginning_pages", side_effect=fake_verify),
    ):
        match, attempts, visual_calls, result = _locate_with_react(
            path_titles=("Appendix A",),
            title="Appendix A",
            left=250,
            right=300,
            body_pages=list(range(250, 301)),
            ctx=ctx,
        )

    assert match is not None
    assert match.page == 262
    assert result == "react_line_grep_vlm"
    assert verified_batches == [[260], [262]]
    assert visual_calls == 2
    assert attempts[0]["observation"] == "section_start_confirmed"
    assert attempts[0]["visual_pages_checked"] == [260, 262]
    assert attempts[0]["visual_rounds"][0]["physical_window"] == [260, 261]
    assert attempts[0]["visual_rounds"][1]["physical_window"] == [262, 265]


def test_react_planner_grep_budget_is_not_page_constant() -> None:
    """M5: dedicated grep budget; seed free; max turns = budget + 2 (strips)."""
    from app.services.document_agent.structure import null_page_react as npr
    from app.services.document_agent.structure.null_page_react import (
        REACT_PLANNER_GREP_BUDGET,
        _locate_with_react,
        react_budget,
    )

    assert REACT_PLANNER_GREP_BUDGET == 5
    assert react_budget() == REACT_PLANNER_GREP_BUDGET

    ctx = _ctx()
    planner_turns = {"n": 0}

    def fake_grep(**kwargs: Any) -> tuple[str, str, list[int], int]:
        # Seed + any strip re-grep: empty hits, never consume via consume_budget=False path
        return "ok", "needle", [], 0

    def fake_propose(**kwargs: Any) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        planner_turns["n"] += 1
        return None, {"error": "planner failed"}

    with (
        patch.object(npr, "react_budget", return_value=1),
        patch.object(npr, "_whole_line_grep", side_effect=fake_grep),
        patch.object(npr, "_propose_react_query", side_effect=fake_propose),
    ):
        _match, attempts, _visual, result = _locate_with_react(
            path_titles=("T",),
            title="T",
            left=1,
            right=5,
            body_pages=[1, 2, 3, 4, 5],
            ctx=ctx,
        )

    # max_planner_turns = budget + 2 = 3 (not budget + 2 + budget).
    assert planner_turns["n"] == 3
    assert result == "react_loop_limit"
    assert all(a.get("action") == "planner_error" or a.get("action") == "grep" for a in attempts)

def test_null_page_grep_observations_distinguish_error_empty_duplicate() -> None:
    """M1: tool error / empty needle / true duplicate are distinct labels."""
    from app.services.document_agent.structure import null_page_react as npr
    from app.services.document_agent.structure.null_page_react import _locate_with_react

    ctx = _ctx()
    calls = {"n": 0}

    def fake_grep(**kwargs: Any) -> tuple[str, str, list[int], int]:
        calls["n"] += 1
        if calls["n"] == 1:
            return "error", "grep.text failed", [], 0
        if calls["n"] == 2:
            return "ok", "", [], 0
        if calls["n"] == 3:
            return "ok", "same needle", [5], 1
        return "ok", "same needle", [5], 1

    proposals = iter(
        [
            ({"action": "grep", "query": "q2"}, {}),
            ({"action": "grep", "query": "q3"}, {}),
            ({"action": "grep", "query": "q3 again"}, {}),
            ({"action": "give_up", "query": ""}, {}),
        ]
    )

    def fake_propose(**kwargs: Any) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        return next(proposals)

    def fake_verify(**kwargs: Any) -> tuple[str, int | None, str, int]:
        return "ok", None, "reject", 0

    with (
        patch.object(npr, "_whole_line_grep", side_effect=fake_grep),
        patch.object(npr, "_propose_react_query", side_effect=fake_propose),
        patch.object(npr, "_verify_section_beginning_pages", side_effect=fake_verify),
        patch.object(npr, "react_budget", return_value=5),
    ):
        _match, attempts, _visual, _result = _locate_with_react(
            path_titles=("T",),
            title="T",
            left=1,
            right=10,
            body_pages=list(range(1, 11)),
            ctx=ctx,
        )

    observations = [a.get("observation") for a in attempts if a.get("action") == "grep"]
    assert observations[0] == "grep_tool_error"
    assert observations[1] == "empty_normalized_query"
    assert observations[2] == "visual_rejected"
    assert observations[3] == "duplicate_normalized_query"


def test_null_page_locate_summary_splits_leaf_and_parent() -> None:
    """M2: C4 locate_summary keeps leaf and parent buckets separate."""
    from app.services.page_memory.skeleton_extractor import _null_page_locate_bucket

    report = [
        {
            "kind": "leaf",
            "page": 5,
            "result": "react_line_grep_vlm",
            "visual_verify_calls": 1,
        },
        {
            "kind": "leaf",
            "page": None,
            "result": "unresolved",
            "visual_verify_calls": 0,
        },
        {
            "kind": "parent",
            "page": 4,
            "result": "react_line_grep_vlm",
            "visual_verify_calls": 2,
        },
    ]
    leaf = _null_page_locate_bucket(report, kind="leaf")
    parent = _null_page_locate_bucket(report, kind="parent")
    assert leaf["attempted"] == 2
    assert leaf["located"] == 1
    assert leaf["unresolved"] == 1
    assert leaf["visual_verify_calls"] == 1
    assert parent["attempted"] == 1
    assert parent["located"] == 1
    assert parent["visual_verify_calls"] == 2
    assert [e["kind"] for e in leaf["entries"]] == ["leaf", "leaf"]
    assert [e["kind"] for e in parent["entries"]] == ["parent"]


def test_null_page_react_hit_writes_override() -> None:
    from app.services.document_agent.structure import null_page_react as npr
    from app.services.document_agent.structure.null_page_react import (
        locate_null_page_overrides,
    )

    leaf = TitleNode(title="Appendix B", level=1, printed_page=None, children=[])
    ctx = _ctx()
    match = TitleMatch(
        page=12,
        source="react_line_grep_vlm",
        matched_line="Appendix B",
        candidates=[12],
        evidence={
            "accept": "react_line_grep_vlm",
            "null_page_react": True,
            "normalized_query": "appendix b",
        },
    )

    def fake_react(**kwargs: Any) -> tuple[TitleMatch | None, list[dict[str, Any]], int, str]:
        assert kwargs["left"] == 1
        assert kwargs["right"] == 20
        return match, [{"loop": 1, "observation": "section_start_confirmed"}], 1, (
            "react_line_grep_vlm"
        )

    with patch.object(npr, "_locate_with_react", side_effect=fake_react):
        overrides, report = locate_null_page_overrides(
            nodes=[leaf],
            match_overrides={},
            body_pages=list(range(1, 21)),
            ctx=ctx,
        )

    assert overrides[("Appendix B",)].page == 12
    assert overrides[("Appendix B",)].source == "react_line_grep_vlm"
    assert report[0]["page"] == 12
    assert report[0]["result"] == "react_line_grep_vlm"


def test_null_page_parent_without_descendant_anchor_uses_inherited_scope() -> None:
    from app.services.document_agent.structure.null_page_react import (
        locate_null_page_overrides,
    )

    parent = TitleNode(
        title="Chapter",
        level=1,
        printed_page=None,
        children=[TitleNode(title="Orphan", level=2, printed_page=None, children=[])],
    )
    overrides, report = locate_null_page_overrides(
        nodes=[parent],
        match_overrides={},
        body_pages=[1, 2, 3],
        ctx=None,
    )
    assert overrides == {}
    assert report[0]["path_titles"] == ["Chapter"]
    assert report[0]["search_scope"] == [1, 3]
    assert report[0]["result"] == "unresolved_no_ctx"


def test_null_page_parent_uses_first_descendant_as_right_boundary() -> None:
    from app.services.document_agent.structure import null_page_react as npr
    from app.services.document_agent.structure.null_page_react import (
        locate_null_page_overrides,
    )

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
    def probe(**kwargs: Any) -> tuple[TitleMatch, list[dict[str, Any]], int, str]:
        assert kwargs["title"] == "1 Overview"
        assert kwargs["left"] == 4
        assert kwargs["right"] == 5
        return (
            TitleMatch(
                page=4,
                source="react_line_grep_vlm",
                matched_line="1 Overview",
                candidates=[4],
                evidence={"accept": "react_line_grep_vlm"},
            ),
            [],
            1,
            "react_line_grep_vlm",
        )

    with patch.object(npr, "_locate_with_react", side_effect=probe):
        overrides, report = locate_null_page_overrides(
            nodes=[parent],
            match_overrides=leaf_match,
            body_pages=[4, 5, 6],
            ctx=_ctx(),
        )
    assert ("1 Overview",) in overrides
    assert overrides[("1 Overview",)].page == 4
    assert report[0]["search_scope"] == [4, 5]


def test_root_first_null_parent_uses_body_start_as_left() -> None:
    from app.services.document_agent.structure import null_page_react as npr
    from app.services.document_agent.structure.null_page_react import (
        locate_null_page_overrides,
    )

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
            source="anchored",
            matched_line="",
            candidates=[278],
            evidence={},
        )
    }
    body_pages = list(range(1, 301))

    def probe(**kwargs: Any) -> tuple[TitleMatch, list[dict[str, Any]], int, str]:
        assert kwargs["left"] == 1
        assert kwargs["right"] == 278
        return (
            TitleMatch(
                page=270,
                source="react_line_grep_vlm",
                matched_line="Chapter 22",
                candidates=[270],
                evidence={"accept": "react_line_grep_vlm"},
            ),
            [],
            1,
            "react_line_grep_vlm",
        )

    with patch.object(npr, "_locate_with_react", side_effect=probe):
        overrides, report = locate_null_page_overrides(
            nodes=[parent],
            match_overrides=leaf_match,
            body_pages=body_pages,
            ctx=_ctx(),
        )

    assert overrides[("Chapter 22",)].page == 270
    assert report[0]["search_scope"] == [1, 278]


def test_null_child_of_unanchored_printed_parent_uses_preorder_cursor() -> None:
    from app.services.document_agent.structure import null_page_react as npr
    from app.services.document_agent.structure.null_page_react import (
        locate_null_page_overrides,
    )

    prior = TitleNode(title="E11 Utilities", level=1, printed_page=250, children=[])
    parent = TitleNode(
        title="Appendices",
        level=1,
        printed_page=261,
        children=[
            TitleNode(
                title="A City palette maps",
                level=2,
                printed_page=None,
                children=[],
            )
        ],
    )

    def probe(**kwargs: Any) -> tuple[None, list[dict[str, Any]], int, str]:
        assert kwargs["left"] == 250
        assert kwargs["right"] == 443
        return None, [], 0, "react_give_up"

    with patch.object(npr, "_locate_with_react", side_effect=probe):
        _overrides, report = locate_null_page_overrides(
            nodes=[prior, parent],
            match_overrides={
                ("E11 Utilities",): TitleMatch(
                    page=250,
                    source="bulk_offset",
                    matched_line="",
                    candidates=[250],
                    evidence={"offset": 0, "printed_page": 250},
                )
            },
            body_pages=list(range(250, 444)),
            ctx=_ctx(),
        )

    assert report[0]["search_scope"] == [250, 443]


def test_null_page_parent_uses_previous_sibling_last_leaf_as_left() -> None:
    from app.services.document_agent.structure import null_page_react as npr
    from app.services.document_agent.structure.null_page_react import (
        locate_null_page_overrides,
    )

    left_child = TitleNode(title="A.1", level=2, printed_page=10, children=[])
    left = TitleNode(title="A", level=1, printed_page=10, children=[left_child])
    right_child = TitleNode(title="B.1", level=2, printed_page=50, children=[])
    right = TitleNode(title="B", level=1, printed_page=None, children=[right_child])
    overrides_in = {
        ("A",): TitleMatch(
            page=10,
            source="anchored",
            matched_line="",
            candidates=[10],
            evidence={},
        ),
        ("A", "A.1"): TitleMatch(
            page=10,
            source="anchored",
            matched_line="",
            candidates=[10],
            evidence={},
        ),
        ("B", "B.1"): TitleMatch(
            page=50,
            source="anchored",
            matched_line="",
            candidates=[50],
            evidence={},
        ),
    }
    def probe(**kwargs: Any) -> tuple[TitleMatch, list[dict[str, Any]], int, str]:
        assert kwargs["left"] == 10
        assert kwargs["right"] == 50
        return (
            TitleMatch(
                page=40,
                source="react_line_grep_vlm",
                matched_line="B",
                candidates=[40],
                evidence={"accept": "react_line_grep_vlm"},
            ),
            [],
            1,
            "react_line_grep_vlm",
        )

    with patch.object(npr, "_locate_with_react", side_effect=probe):
        overrides, report = locate_null_page_overrides(
            nodes=[left, right],
            match_overrides=overrides_in,
            body_pages=list(range(1, 61)),
            ctx=_ctx(),
        )

    assert overrides[("B",)].page == 40
    assert report[0]["search_scope"] == [10, 50]


def test_confirmed_null_parent_becomes_child_left_boundary() -> None:
    from app.services.document_agent.structure import null_page_react as npr
    from app.services.document_agent.structure.null_page_react import (
        locate_null_page_overrides,
    )

    detail = TitleNode(title="3.1.1 Detail", level=3, printed_page=304, children=[])
    child = TitleNode(
        title="3.1 Street Furniture with Advertising",
        level=2,
        printed_page=None,
        children=[detail],
    )
    parent = TitleNode(
        title="3 Advertising",
        level=1,
        printed_page=None,
        children=[child],
    )
    overrides_in = {
        ("3 Advertising", "3.1 Street Furniture with Advertising", "3.1.1 Detail"): TitleMatch(
            page=304,
            source="anchored",
            matched_line="",
            candidates=[304],
            evidence={},
        )
    }
    scopes: list[tuple[str, int, int]] = []

    def probe(**kwargs: Any) -> tuple[TitleMatch, list[dict[str, Any]], int, str]:
        title = str(kwargs["title"])
        scopes.append((title, int(kwargs["left"]), int(kwargs["right"])))
        return (
            TitleMatch(
                page=304,
                source="react_line_grep_vlm",
                matched_line=title,
                candidates=[304],
                evidence={"accept": "react_line_grep_vlm"},
            ),
            [],
            1,
            "react_line_grep_vlm",
        )

    with patch.object(npr, "_locate_with_react", side_effect=probe):
        overrides, report = locate_null_page_overrides(
            nodes=[parent],
            match_overrides=overrides_in,
            body_pages=list(range(280, 330)),
            ctx=_ctx(),
        )

    assert scopes == [
        ("3 Advertising", 280, 304),
        ("3.1 Street Furniture with Advertising", 304, 304),
    ]
    assert overrides[("3 Advertising",)].page == 304
    assert overrides[("3 Advertising", "3.1 Street Furniture with Advertising")].page == 304
    assert [row["kind"] for row in report] == ["parent", "parent"]


def test_production_flow_calls_one_parent_first_null_page_walker() -> None:
    child = TitleNode(title="Child", level=2, printed_page=None, children=[])
    parent = TitleNode(
        title="Parent",
        level=1,
        printed_page=None,
        children=[child],
    )
    calls: list[str] = []

    def fake_locate(
        **kwargs: Any,
    ) -> tuple[dict[tuple[str, ...], TitleMatch], list[dict[str, Any]]]:
        calls.append("unified")
        overrides = dict(kwargs["match_overrides"])
        overrides[("Parent",)] = TitleMatch(
            page=4,
            source="anchored",
            matched_line="Parent",
            candidates=[4],
            evidence={},
        )
        overrides[("Parent", "Child")] = TitleMatch(
            page=5,
            source="anchored",
            matched_line="Child",
            candidates=[5],
            evidence={},
        )
        return overrides, [
            {"kind": "parent", "page": 4},
            {"kind": "leaf", "page": 5},
        ]

    with patch.object(
        anchoring, "locate_null_page_overrides", side_effect=fake_locate
    ):
        resolved, anchor = anchoring.anchor_hierarchy_from_offset(
            nodes=[parent],
            offset_hint=None,
            calibration_overrides={},
            page_texts={page: "noise" for page in range(1, 11)},
            body_pages=list(range(1, 11)),
            page_count=10,
            ctx=None,
        )

    assert calls == ["unified"]
    assert [node.title for node in resolved] == ["Parent"]
    assert set(anchor.match_overrides) == {("Parent",), ("Parent", "Child")}
    assert [row["kind"] for row in anchor.null_page_report] == ["parent", "leaf"]


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


def test_regimes_bulk_count_excludes_null_page_react_hits() -> None:
    """Bulk count freezes before unified ReAct; its hits stay in the report."""
    from app.services.document_agent.calibration import procedure as proc
    from app.services.document_agent.calibration.procedure import (
        anchor_hierarchy_from_regimes,
    )
    from app.services.document_agent.calibration.types import (
        CalibrationRegime,
        CalibrationResult,
        CalibrationSample,
    )

    nodes = [
        _leaf("Ch1", 1),
        TitleNode(title="NullLeaf", level=1, printed_page=None, children=[]),
    ]
    phase1 = CalibrationResult(
        status="ok",
        offset=10,
        regimes=[
            CalibrationRegime(
                kind="decimal",
                offset=10,
                offset_status="ok",
                entry_indices=[0, 1],
                samples=[CalibrationSample(title="Ch1", physical=11)],
            )
        ],
    )
    bulk_match = TitleMatch(
        page=11,
        source="bulk_offset",
        matched_line="Ch1",
        candidates=[11],
        evidence={},
    )
    react_match = TitleMatch(
        page=20,
        source="react_line_grep_vlm",
        matched_line="NullLeaf",
        candidates=[20],
        evidence={"accept": "react_line_grep_vlm"},
    )

    def fake_offset(**kwargs: Any) -> dict[tuple[str, ...], TitleMatch]:
        return {("Ch1",): bulk_match}

    def fake_apply(**kwargs: Any) -> tuple[list, dict, list, int, int]:
        out = dict(kwargs["match_overrides"])
        pre_react = len(out)
        out[("NullLeaf",)] = react_match
        return (
            list(kwargs["nodes"]),
            out,
            [
                {
                    "path_titles": ["NullLeaf"],
                    "kind": "leaf",
                    "page": 20,
                    "result": "react_line_grep_vlm",
                }
            ],
            0,
            pre_react,
        )

    with (
        patch.object(proc, "offset_guided_anchoring", side_effect=fake_offset),
        patch.object(proc, "apply_null_page_locates_and_prune", side_effect=fake_apply),
    ):
        _working, anchor = anchor_hierarchy_from_regimes(
            nodes=nodes,
            result=phase1,
            entries=[
                {"heading": "Ch1", "level": 1, "page_number": 1},
                {"heading": "NullLeaf", "level": 1, "page_number": None},
            ],
            page_texts={11: "Ch1", 20: "NullLeaf"},
            body_pages=list(range(1, 30)),
            page_count=30,
            ctx=_ctx(),
        )

    assert ("Ch1",) in anchor.match_overrides
    assert ("NullLeaf",) in anchor.match_overrides
    assert len(anchor.match_overrides) == 2
    assert anchor.bulk_count == 1
    assert any(
        row.get("path_titles") == ["NullLeaf"] for row in anchor.null_page_report
    )


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


def test_printed_backfill_uses_nearest_doc_order_offset() -> None:
    from app.services.document_agent.structure import anchoring_primitives as primitives

    early_child = TitleNode(title="A.1", level=2, printed_page=12, children=[])
    late_child = TitleNode(title="B.1", level=2, printed_page=42, children=[])
    section_a = _printed_page_parent("Section A", 10, early_child)
    section_b = _printed_page_parent("Section B", 40, late_child)
    matches = {
        **primitives.bulk_offset_matches([(("Section A", "A.1"), early_child)], 5),
        **primitives.bulk_offset_matches([(("Section B", "B.1"), late_child)], 9),
    }

    parents = primitives.backfill_printed_offset_matches(
        nodes=[section_a, section_b],
        matches=matches,
        page_count=60,
    )

    assert parents[("Section A",)].page == 15
    assert parents[("Section B",)].page == 49
    assert parents[("Section A",)].evidence["printed_offset_backfill"] is True


def test_printed_backfill_ignores_react_hits_without_offset() -> None:
    """ReAct leaves carry no offset; nearest offset-bearing neighbor wins."""
    from app.services.document_agent.structure import anchoring_primitives as primitives

    react_child = TitleNode(title="Intro", level=2, printed_page=None, children=[])
    printed_child = TitleNode(title="Body", level=2, printed_page=12, children=[])
    section = TitleNode(
        title="Section A",
        level=1,
        printed_page=10,
        children=[react_child, printed_child],
    )
    matches = {
        ("Section A", "Intro"): TitleMatch(
            page=99,
            source="react_line_grep_vlm",
            matched_line="Intro",
            candidates=[99],
            evidence={"accept": "react_line_grep_vlm", "null_page_react": True},
        ),
        **primitives.bulk_offset_matches(
            [(("Section A", "Body"), printed_child)], 5
        ),
    }

    parents = primitives.backfill_printed_offset_matches(
        nodes=[section],
        matches=matches,
        page_count=200,
    )

    assert parents[("Section A",)].page == 15
    assert parents[("Section A",)].evidence["offset"] == 5


def test_printed_backfill_unresolved_when_no_offset_bearing_neighbor() -> None:
    from app.services.document_agent.structure import anchoring_primitives as primitives

    react_child = TitleNode(title="Intro", level=2, printed_page=None, children=[])
    section = _printed_page_parent("Section A", 10, react_child)
    matches = {
        ("Section A", "Intro"): TitleMatch(
            page=99,
            source="react_line_grep_vlm",
            matched_line="Intro",
            candidates=[99],
            evidence={"accept": "react_line_grep_vlm", "null_page_react": True},
        )
    }

    parents = primitives.backfill_printed_offset_matches(
        nodes=[section],
        matches=matches,
        page_count=200,
    )

    assert parents == {}


def test_printed_backfill_uses_preceding_neighbor_and_skips_out_of_range() -> None:
    from app.services.document_agent.structure import anchoring_primitives as primitives

    prior_leaf = TitleNode(title="E11", level=1, printed_page=250, children=[])
    appendices = TitleNode(
        title="Appendices",
        level=1,
        printed_page=261,
        children=[
            TitleNode(title="A maps", level=2, printed_page=None, children=[]),
        ],
    )
    tail_child = TitleNode(title="Tail.1", level=2, printed_page=440, children=[])
    tail = _printed_page_parent("Tail", 439, tail_child)
    matches = {
        **primitives.bulk_offset_matches([(("E11",), prior_leaf)], 0),
        **primitives.bulk_offset_matches([(("Tail", "Tail.1"), tail_child)], 8),
    }

    filled = primitives.backfill_printed_offset_matches(
        nodes=[prior_leaf, appendices, tail],
        matches=matches,
        page_count=443,
    )

    assert filled[("Appendices",)].page == 261
    assert filled[("Appendices",)].evidence["offset"] == 0
    # 439 + 8 = 447 exceeds page_count
    assert ("Tail",) not in filled


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