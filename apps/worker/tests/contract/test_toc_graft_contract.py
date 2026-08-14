"""Contract tests for contained TOC graft. Synthetic trees only; no PDF."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.budget import BudgetTracker
from app.services.document_agent.manifest import (
    PageAnatomyMap,
    PageFeature,
    PageLabel,
    Shard,
    ShardPlan,
    TocResult,
    ToolContext,
)
from app.services.document_agent.state import AgentBlackboard
from app.services.document_agent.structure.anchoring_primitives import (
    SkeletonAnchor,
    serialize_skeleton_anchor,
    serialize_title_node,
)
from app.services.document_agent.structure.hierarchy_locator import (
    TitleMatch,
    TitleNode,
)
from app.services.document_agent.structure.toc_anchoring import run_toc_anchoring
from app.services.document_agent.structure.toc_graft import graft_contained_toc
from app.services.page_memory.skeleton_extractor import extract_section_skeletons


def _match(title: str, page: int) -> TitleMatch:
    return TitleMatch(
        page=page,
        confidence=1.0,
        source="anchored",
        matched_line=title,
        score=1.0,
        candidates=[page],
        evidence={},
    )


def _overrides(pages: dict[tuple[str, ...], int]) -> dict[tuple[str, ...], TitleMatch]:
    return {path: _match(path[-1], page) for path, page in pages.items()}


def _graft(
    *,
    primary: list[TitleNode],
    primary_pages: dict[tuple[str, ...], int],
    contained: list[TitleNode],
    contained_pages: dict[tuple[str, ...], int],
    page_count: int = 50,
) -> object:
    body_pages = list(range(1, page_count + 1))
    page_texts = {page: "" for page in body_pages}
    return graft_contained_toc(
        primary_nodes=primary,
        primary_overrides=_overrides(primary_pages),
        contained_nodes=contained,
        contained_overrides=_overrides(contained_pages),
        page_count=page_count,
        page_texts=page_texts,
        body_pages=body_pages,
    )


def _node_at(nodes: list[TitleNode], path: tuple[str, ...]) -> TitleNode | None:
    current = nodes
    found: TitleNode | None = None
    for title in path:
        found = next((node for node in current if node.title == title), None)
        if found is None:
            return None
        current = list(found.children)
    return found


def test_dedup_keeps_primary_title_and_hangs_child() -> None:
    result = _graft(
        primary=[TitleNode(title="第一章", level=1, printed_page=10)],
        primary_pages={("第一章",): 10},
        contained=[
            TitleNode(
                title="第一章",
                level=1,
                printed_page=10,
                children=[TitleNode(title="1.2", level=2, printed_page=12)],
            )
        ],
        contained_pages={("第一章",): 10, ("第一章", "1.2"): 12},
    )

    assert [node.title for node in result.nodes] == ["第一章"]
    child = _node_at(result.nodes, ("第一章", "1.2"))
    assert child is not None
    assert child.level == 2
    assert result.match_overrides[("第一章",)].page == 10
    assert result.match_overrides[("第一章", "1.2")].page == 12
    dedup = next(event for event in result.events if event["action"] == "dedup")
    assert dedup["title_equal"] is True


def test_dedup_ignores_title_and_keeps_primary_override() -> None:
    primary_match = _match("第一章", 10)
    body_pages = list(range(1, 51))
    result = graft_contained_toc(
        primary_nodes=[TitleNode(title="第一章", level=1, printed_page=10)],
        primary_overrides={("第一章",): primary_match},
        contained_nodes=[
            TitleNode(
                title="Chapter 1",
                level=1,
                printed_page=10,
                children=[TitleNode(title="1.2", level=2, printed_page=12)],
            )
        ],
        contained_overrides=_overrides({("Chapter 1",): 10, ("Chapter 1", "1.2"): 12}),
        page_count=50,
        page_texts={page: "" for page in body_pages},
        body_pages=body_pages,
    )

    assert [node.title for node in result.nodes] == ["第一章"]
    assert _node_at(result.nodes, ("第一章", "1.2")) is not None
    assert result.match_overrides[("第一章",)] is primary_match
    assert ("Chapter 1",) not in result.match_overrides
    dedup = next(event for event in result.events if event["action"] == "dedup")
    assert dedup["title_equal"] is False


def test_attach_new_child_rebases_level() -> None:
    result = _graft(
        primary=[TitleNode(title="第一章", level=1, printed_page=10)],
        primary_pages={("第一章",): 10},
        contained=[TitleNode(title="1.2", level=1, printed_page=12)],
        contained_pages={("1.2",): 12},
    )

    child = _node_at(result.nodes, ("第一章", "1.2"))
    assert child is not None
    assert child.level == 2
    assert result.match_overrides[("第一章", "1.2")].page == 12
    assert any(event["action"] == "attach" for event in result.events)


def test_same_start_parent_and_child_do_not_collapse() -> None:
    result = _graft(
        primary=[TitleNode(title="1.1", level=1, printed_page=10)],
        primary_pages={("1.1",): 10},
        contained=[
            TitleNode(
                title="1.1",
                level=1,
                printed_page=10,
                children=[TitleNode(title="1.1.1", level=2, printed_page=10)],
            )
        ],
        contained_pages={("1.1",): 10, ("1.1", "1.1.1"): 10},
    )

    parent = _node_at(result.nodes, ("1.1",))
    child = _node_at(result.nodes, ("1.1", "1.1.1"))
    assert parent is not None
    assert child is not None
    assert parent.title == "1.1"
    assert child.title == "1.1.1"
    assert [node.title for node in result.nodes] == ["1.1"]


def test_two_contained_tocs_graft_in_order() -> None:
    first = _graft(
        primary=[TitleNode(title="第一章", level=1, printed_page=10)],
        primary_pages={("第一章",): 10},
        contained=[
            TitleNode(
                title="第一章",
                level=1,
                printed_page=10,
                children=[TitleNode(title="1.2", level=2, printed_page=12)],
            )
        ],
        contained_pages={("第一章",): 10, ("第一章", "1.2"): 12},
    )
    second = graft_contained_toc(
        primary_nodes=first.nodes,
        primary_overrides=first.match_overrides,
        contained_nodes=[
            TitleNode(
                title="第一章",
                level=1,
                printed_page=10,
                children=[TitleNode(title="1.3", level=2, printed_page=20)],
            )
        ],
        contained_overrides=_overrides({("第一章",): 10, ("第一章", "1.3"): 20}),
        page_count=50,
        page_texts={page: "" for page in range(1, 51)},
        body_pages=list(range(1, 51)),
    )

    titles = [child.title for child in second.nodes[0].children]
    assert titles == ["1.2", "1.3"]


def _ctx(*, page_count: int) -> ToolContext:
    return ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="job-graft",
        blackboard=AgentBlackboard(page_count=page_count),
        budget=BudgetTracker(plan_budget=50_000, visual_budget=80_000),
        trace=None,
        settings={},
    )


def _anchor(pages: dict[tuple[str, ...], int]) -> SkeletonAnchor:
    return SkeletonAnchor(
        offset=0,
        offset_status="ok",
        match_overrides=_overrides(pages),
        null_page_report=[],
        bulk_count=len(pages),
        pruned_count=0,
        locate_agent="offset_guided_bulk",
    )


def test_parallel_pending_is_not_grafted() -> None:
    ctx = _ctx(page_count=30)
    ctx.blackboard.toc_hierarchies = [
        {
            "toc_range": [1, 1],
            "toc_range_unit": "page",
            "toc_with_level": [{"heading": "Ch1", "level": 1, "page_number": 2}],
        },
        {
            "toc_range": [20, 21],
            "toc_range_unit": "page",
            "toc_with_level": [{"heading": "App", "level": 1, "page_number": 22}],
        },
    ]
    ctx.blackboard.toc_result = TocResult(method="vlm_batch", toc_pages=[1, 20, 21])
    ctx.blackboard.page_full_text_cache = {page: "body" for page in range(1, 31)}
    primary = TitleNode(title="Ch1", level=1, printed_page=2)
    pending = TitleNode(title="App", level=1, printed_page=22)
    captured: dict[str, object] = {}

    def fake_anchor_hierarchy(**kwargs):
        captured["body_pages"] = kwargs["body_pages"]
        return [primary], _anchor({("Ch1",): 2})

    with (
        patch(
            "app.services.document_agent.structure.toc_anchoring.anchor_hierarchy",
            side_effect=fake_anchor_hierarchy,
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring.calibrate_offset",
            return_value=object(),
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring.pick_primary_offset",
            return_value=0,
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring.classify_toc_relationship",
            return_value="parallel",
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring.finalize_calibration_result",
            return_value=([pending], _anchor({("App",): 22}), True),
        ),
    ):
        run_toc_anchoring(ctx)

    assert 22 in captured["body_pages"]
    assert [node["title"] for node in ctx.blackboard.skeleton_nodes] == ["Ch1"]
    record = ctx.blackboard.pending_skeleton_anchors[0]
    assert record["relationship"] == "parallel"
    assert "grafted" not in record


def test_profile_grafts_contained_and_keeps_original_pending() -> None:
    ctx = _ctx(page_count=50)
    ctx.blackboard.toc_hierarchies = [
        {
            "toc_range": [1, 1],
            "toc_range_unit": "page",
            "toc_with_level": [{"heading": "第一章", "level": 1, "page_number": 10}],
        },
        {
            "toc_range": [20, 21],
            "toc_range_unit": "page",
            "toc_with_level": [
                {"heading": "第一章", "level": 1, "page_number": 10},
                {"heading": "1.2", "level": 2, "page_number": 12},
            ],
        },
    ]
    ctx.blackboard.toc_result = TocResult(method="vlm_batch", toc_pages=[1, 20, 21])
    ctx.blackboard.page_full_text_cache = {page: "body" for page in range(1, 51)}
    primary = TitleNode(title="第一章", level=1, printed_page=10)
    contained = TitleNode(
        title="第一章",
        level=1,
        printed_page=10,
        children=[TitleNode(title="1.2", level=2, printed_page=12)],
    )

    with (
        patch(
            "app.services.document_agent.structure.toc_anchoring.anchor_hierarchy",
            return_value=([primary], _anchor({("第一章",): 10})),
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring.calibrate_offset",
            return_value=object(),
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring.pick_primary_offset",
            return_value=0,
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring.classify_toc_relationship",
            return_value="contained",
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring.finalize_calibration_result",
            return_value=(
                [contained],
                _anchor({("第一章",): 10, ("第一章", "1.2"): 12}),
                True,
            ),
        ),
    ):
        run_toc_anchoring(ctx)

    assert ctx.blackboard.skeleton_nodes[0]["title"] == "第一章"
    assert ctx.blackboard.skeleton_nodes[0]["children"][0]["title"] == "1.2"
    record = ctx.blackboard.pending_skeleton_anchors[0]
    assert record["grafted"] is True
    assert record["nodes"][0]["title"] == "第一章"
    assert "第一章 / 1.2" in ctx.blackboard.skeleton_anchor["match_overrides"]


def _feature(page: int) -> PageFeature:
    return PageFeature(
        page=page,
        raw_text_length=20,
        text_density=0.1,
        image_coverage=0.0,
        image_count=0,
        table_count=0,
        drawings_count=0,
        orientation="portrait",
        width=72.0,
        height=72.0,
        has_asset=False,
        is_blank_like=False,
    )


def _anatomy(
    *,
    page_count: int,
    nodes: list[TitleNode],
    overrides: dict[tuple[str, ...], int],
    hierarchies: list[dict[str, object]],
    pending_records: list[dict[str, object]],
    toc_pages: list[int],
) -> PageAnatomyMap:
    return PageAnatomyMap(
        job_id="job-graft",
        file_path="/tmp/doc.pdf",
        page_count=page_count,
        page_features=[_feature(page) for page in range(1, page_count + 1)],
        page_labels=[
            PageLabel(page=page, kind="normal", confidence=1.0)
            for page in range(1, page_count + 1)
        ],
        toc_result=TocResult(method="vlm_batch", toc_pages=toc_pages),
        shard_plan=ShardPlan(
            enabled=False,
            reason="not_needed",
            shards=[
                Shard(
                    shard_index=0,
                    page_start=1,
                    page_end=page_count,
                    page_offset=0,
                    anchor_type="forced_max_size",
                    anchor_evidence="test",
                    confidence=1.0,
                )
            ],
        ),
        toc_hierarchies=hierarchies,
        toc_page_offset=0,
        skeleton_anchor=serialize_skeleton_anchor(_anchor(overrides)),
        skeleton_nodes=[serialize_title_node(node) for node in nodes],
        pending_skeleton_anchors=pending_records,
    )


def _pending_record(
    *,
    toc: dict[str, object],
    relationship: str,
    nodes: list[TitleNode],
    overrides: dict[tuple[str, ...], int],
    grafted: bool = False,
) -> dict[str, object]:
    record: dict[str, object] = {
        "toc": toc,
        "relationship": relationship,
        "nodes": [serialize_title_node(node) for node in nodes],
        "skeleton_anchor": serialize_skeleton_anchor(_anchor(overrides)),
    }
    if grafted:
        record["grafted"] = True
        record["graft"] = []
    return record


def test_page_contained_does_not_cut_primary_window() -> None:
    pending_toc = {
        "toc_range": [200, 201],
        "toc_range_unit": "page",
        "toc_with_level": [{"heading": "Inner", "level": 1, "page_number": 210}],
    }
    anatomy = _anatomy(
        page_count=250,
        nodes=[
            TitleNode(title="Ch1", level=1, printed_page=3),
            TitleNode(title="Ch2", level=1, printed_page=120),
        ],
        overrides={("Ch1",): 3, ("Ch2",): 120},
        hierarchies=[
            {
                "toc_range": [1, 2],
                "toc_range_unit": "page",
                "toc_with_level": [
                    {"heading": "Ch1", "level": 1, "page_number": 3},
                    {"heading": "Ch2", "level": 1, "page_number": 120},
                ],
            },
            pending_toc,
        ],
        pending_records=[
            _pending_record(
                toc=pending_toc,
                relationship="contained",
                nodes=[TitleNode(title="Inner", level=1, printed_page=210)],
                overrides={("Inner",): 210},
                grafted=True,
            )
        ],
        toc_pages=[1, 2, 200, 201],
    )
    page_texts = {page: "body" for page in range(1, 251)}
    skeletons = extract_section_skeletons(
        anatomy=anatomy,
        filename="doc.pdf",
        page_texts=page_texts,
    )
    ch2 = next(item for item in skeletons if item.title == "Ch2")
    assert ch2.end_page > 201
    assert all(item.title != "Inner" for item in skeletons)


def test_page_parallel_still_cuts_primary_window() -> None:
    pending_toc = {
        "toc_range": [200, 201],
        "toc_range_unit": "page",
        "toc_with_level": [{"heading": "App", "level": 1, "page_number": 210}],
    }
    anatomy = _anatomy(
        page_count=250,
        nodes=[
            TitleNode(title="Ch1", level=1, printed_page=3),
            TitleNode(title="Ch2", level=1, printed_page=120),
        ],
        overrides={("Ch1",): 3, ("Ch2",): 120},
        hierarchies=[
            {
                "toc_range": [1, 2],
                "toc_range_unit": "page",
                "toc_with_level": [
                    {"heading": "Ch1", "level": 1, "page_number": 3},
                    {"heading": "Ch2", "level": 1, "page_number": 120},
                ],
            },
            pending_toc,
        ],
        pending_records=[
            _pending_record(
                toc=pending_toc,
                relationship="parallel",
                nodes=[TitleNode(title="App", level=1, printed_page=210)],
                overrides={("App",): 210},
            )
        ],
        toc_pages=[1, 2, 200, 201],
    )
    page_texts = {page: "body" for page in range(1, 251)}
    page_texts[210] = "App"
    skeletons = extract_section_skeletons(
        anatomy=anatomy,
        filename="doc.pdf",
        page_texts=page_texts,
    )
    ch2 = next(item for item in skeletons if item.title == "Ch2")
    assert ch2.end_page == 199
    assert any(item.title == "App" for item in skeletons)


def test_page_grafted_contained_is_not_flattened() -> None:
    pending_toc = {
        "toc_range": [20, 21],
        "toc_range_unit": "page",
        "toc_with_level": [
            {"heading": "第一章", "level": 1, "page_number": 10},
            {"heading": "1.2", "level": 2, "page_number": 12},
        ],
    }
    anatomy = _anatomy(
        page_count=50,
        nodes=[
            TitleNode(
                title="第一章",
                level=1,
                printed_page=10,
                children=[TitleNode(title="1.2", level=2, printed_page=12)],
            )
        ],
        overrides={("第一章",): 10, ("第一章", "1.2"): 12},
        hierarchies=[
            {
                "toc_range": [1, 1],
                "toc_range_unit": "page",
                "toc_with_level": [
                    {"heading": "第一章", "level": 1, "page_number": 10}
                ],
            },
            pending_toc,
        ],
        pending_records=[
            _pending_record(
                toc=pending_toc,
                relationship="contained",
                nodes=[
                    TitleNode(
                        title="第一章",
                        level=1,
                        printed_page=10,
                        children=[TitleNode(title="1.2", level=2, printed_page=12)],
                    )
                ],
                overrides={("第一章",): 10, ("第一章", "1.2"): 12},
                grafted=True,
            )
        ],
        toc_pages=[1, 20, 21],
    )
    page_texts = {page: "body" for page in range(1, 51)}
    skeletons = extract_section_skeletons(
        anatomy=anatomy,
        filename="doc.pdf",
        page_texts=page_texts,
    )
    titled = [item for item in skeletons if item.title == "1.2"]
    assert len(titled) == 1
    assert "第一章" in titled[0].section_path
