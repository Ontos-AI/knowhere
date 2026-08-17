from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.budget import BudgetTracker
from app.services.document_agent.manifest import PageFeature, TocResult, ToolContext
from app.services.document_agent.state import AgentBlackboard
from app.services.document_agent.structure.anchoring_primitives import (
    SkeletonAnchor,
    serialize_skeleton_anchor,
    serialize_title_node,
)
from app.services.document_agent.structure.hierarchy_locator import TitleMatch, TitleNode
from app.services.document_agent.tools.propose_shard_plan import propose_shard_plan


def _feature(page: int, *, blank: bool = False) -> PageFeature:
    return PageFeature(
        page=page,
        raw_text_length=0 if blank else 20,
        text_density=0.1,
        image_coverage=0.0,
        image_count=0,
        table_count=0,
        drawings_count=0,
        orientation="portrait",
        width=72.0,
        height=72.0,
        has_asset=False,
        is_blank_like=blank,
    )


def _ctx(*, page_count: int, blank_pages: list[int] | None = None) -> ToolContext:
    blanks = set(blank_pages or [])
    ctx = ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="job-shard",
        blackboard=AgentBlackboard(page_count=page_count),
        budget=BudgetTracker(plan_budget=50_000, visual_budget=80_000),
        trace=None,
        settings={
            "shard_threshold": 200,
            "max_pages_per_shard": 200,
        },
    )
    ctx.blackboard.doc_stats = {"page_count": page_count}
    ctx.blackboard.toc_result = TocResult(method="vlm_batch")
    ctx.blackboard.page_features = [
        _feature(page, blank=page in blanks) for page in range(1, page_count + 1)
    ]
    return ctx


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


def _seed_skeleton(
    ctx: ToolContext,
    *,
    nodes: list[TitleNode],
    overrides: dict[tuple[str, ...], int],
    hierarchies: list[dict[str, object]],
    pending_records: list[dict[str, object]] | None = None,
) -> None:
    ctx.blackboard.toc_hierarchies = hierarchies
    ctx.blackboard.skeleton_nodes = [serialize_title_node(node) for node in nodes]
    ctx.blackboard.skeleton_anchor = serialize_skeleton_anchor(
        SkeletonAnchor(
            offset=0,
            offset_status="ok",
            match_overrides={
                path: _match(path[-1], page) for path, page in overrides.items()
            },
            null_page_report=[],
            bulk_count=len(overrides),
            pruned_count=0,
            locate_agent="offset_guided_bulk",
        )
    )
    if pending_records is not None:
        ctx.blackboard.pending_skeleton_anchors = pending_records


def test_hierarchy_pack_cuts_before_next_chapter() -> None:
    ctx = _ctx(page_count=250)
    _seed_skeleton(
        ctx,
        nodes=[
            TitleNode(
                title="Ch1",
                level=1,
                printed_page=3,
                children=[
                    TitleNode(title="1.1", level=2, printed_page=3),
                    TitleNode(title="1.2", level=2, printed_page=80),
                ],
            ),
            TitleNode(title="Ch2", level=1, printed_page=120),
        ],
        overrides={
            ("Ch1",): 3,
            ("Ch1", "1.1"): 3,
            ("Ch1", "1.2"): 80,
            ("Ch2",): 120,
        },
        hierarchies=[
            {
                "toc_range": [1, 2],
                "toc_range_unit": "page",
                "toc_with_level": [
                    {"heading": "Ch1", "level": 1, "page_number": 3},
                    {"heading": "1.1", "level": 2, "page_number": 3},
                    {"heading": "1.2", "level": 2, "page_number": 80},
                    {"heading": "Ch2", "level": 1, "page_number": 120},
                ],
            }
        ],
    )

    result = propose_shard_plan(ctx, {})
    plan = ctx.blackboard.shard_plan

    assert result.status == "ok"
    assert plan is not None
    assert plan.enabled is True
    assert [(shard.page_start, shard.page_end, shard.anchor_type) for shard in plan.shards] == [
        (1, 119, "toc_leaf_boundary"),
        (120, 250, "forced_max_size"),
    ]
    assert plan.validation.valid is True


def test_shard_plan_attaches_calibrated_toc_hierarchies_per_shard() -> None:
    ctx = _ctx(page_count=250)
    _seed_skeleton(
        ctx,
        nodes=[
            TitleNode(
                title="Ch1",
                level=1,
                printed_page=3,
                children=[
                    TitleNode(title="1.1", level=2, printed_page=3),
                    TitleNode(title="1.2", level=2, printed_page=80),
                ],
            ),
            TitleNode(title="Ch2", level=1, printed_page=120),
        ],
        overrides={
            ("Ch1",): 3,
            ("Ch1", "1.1"): 3,
            ("Ch1", "1.2"): 80,
            ("Ch2",): 120,
        },
        hierarchies=[
            {
                "toc_range": [1, 2],
                "toc_range_unit": "page",
                "toc_with_level": [
                    {"heading": "Ch1", "level": 1, "page_number": 3},
                    {"heading": "1.1", "level": 2, "page_number": 3},
                    {"heading": "1.2", "level": 2, "page_number": 80},
                    {"heading": "Ch2", "level": 1, "page_number": 120},
                ],
            }
        ],
    )

    propose_shard_plan(ctx, {})
    plan = ctx.blackboard.shard_plan
    assert plan is not None
    assert len(plan.shards) == 2

    first = plan.shards[0].toc_hierarchies
    second = plan.shards[1].toc_hierarchies
    assert first is not None
    assert second is not None
    assert first[0]["toc_range"] == [1, 119]
    assert second[0]["toc_range"] == [120, 250]
    assert [row["heading"] for row in first[0]["toc_with_level"]] == [
        "Ch1",
        "1.1",
        "1.2",
    ]
    assert [row["heading"] for row in second[0]["toc_with_level"]] == ["Ch2"]
    assert all("page_number" not in row for row in first[0]["toc_with_level"])
    assert all("page_number" not in row for row in second[0]["toc_with_level"])


def test_hierarchy_pack_keeps_same_parent_siblings_together() -> None:
    ctx = _ctx(page_count=250)
    _seed_skeleton(
        ctx,
        nodes=[
            TitleNode(
                title="Ch1",
                level=1,
                printed_page=1,
                children=[
                    TitleNode(title="1.1", level=2, printed_page=1),
                    TitleNode(title="1.2", level=2, printed_page=120),
                ],
            ),
            TitleNode(
                title="Ch2",
                level=1,
                printed_page=150,
                children=[
                    TitleNode(title="2.1", level=2, printed_page=150),
                    TitleNode(title="2.2", level=2, printed_page=180),
                ],
            ),
        ],
        overrides={
            ("Ch1",): 1,
            ("Ch1", "1.1"): 1,
            ("Ch1", "1.2"): 120,
            ("Ch2",): 150,
            ("Ch2", "2.1"): 150,
            ("Ch2", "2.2"): 180,
        },
        hierarchies=[
            {
                "toc_range": [1, 1],
                "toc_range_unit": "page",
                "toc_with_level": [
                    {"heading": "Ch1", "level": 1, "page_number": 1},
                    {"heading": "1.1", "level": 2, "page_number": 1},
                    {"heading": "1.2", "level": 2, "page_number": 120},
                    {"heading": "Ch2", "level": 1, "page_number": 150},
                    {"heading": "2.1", "level": 2, "page_number": 150},
                    {"heading": "2.2", "level": 2, "page_number": 180},
                ],
            }
        ],
    )

    propose_shard_plan(ctx, {})
    plan = ctx.blackboard.shard_plan
    assert plan is not None
    assert [(shard.page_start, shard.page_end) for shard in plan.shards] == [
        (1, 149),
        (150, 250),
    ]


def test_match_override_keeps_non_int_printed_page() -> None:
    ctx = _ctx(page_count=250)
    _seed_skeleton(
        ctx,
        nodes=[
            TitleNode(
                title="Ch1",
                level=1,
                printed_page=1,
                children=[
                    TitleNode(title="A", level=2, printed_page=1),
                    TitleNode(
                        title="B",
                        level=2,
                        printed_page=None,
                        printed_label="iv",
                        page_kind="roman",
                    ),
                    TitleNode(title="C", level=2, printed_page=201),
                ],
            ),
        ],
        overrides={
            ("Ch1",): 1,
            ("Ch1", "A"): 1,
            ("Ch1", "B"): 101,
            ("Ch1", "C"): 201,
        },
        hierarchies=[
            {
                "toc_range": [1, 1],
                "toc_range_unit": "page",
                "toc_with_level": [
                    {"heading": "Ch1", "level": 1, "page_number": 1},
                    {"heading": "A", "level": 2, "page_number": 1},
                    {"heading": "B", "level": 2, "page_number": "iv"},
                    {"heading": "C", "level": 2, "page_number": 201},
                ],
            }
        ],
    )

    propose_shard_plan(ctx, {})
    plan = ctx.blackboard.shard_plan
    assert plan is not None
    assert [(shard.page_start, shard.page_end) for shard in plan.shards] == [
        (1, 100),
        (101, 250),
    ]


def test_pending_toc_forest_is_packed_separately() -> None:
    ctx = _ctx(page_count=350)
    pending_toc = {
        "toc_range": [200, 201],
        "toc_range_unit": "page",
        "toc_with_level": [
            {"heading": "App", "level": 1, "page_number": 210},
        ],
    }
    _seed_skeleton(
        ctx,
        nodes=[
            TitleNode(title="Ch1", level=1, printed_page=3),
            TitleNode(title="Ch2", level=1, printed_page=50),
        ],
        overrides={
            ("Ch1",): 3,
            ("Ch2",): 50,
        },
        hierarchies=[
            {
                "toc_range": [1, 2],
                "toc_range_unit": "page",
                "toc_with_level": [
                    {"heading": "Ch1", "level": 1, "page_number": 3},
                    {"heading": "Ch2", "level": 1, "page_number": 50},
                ],
            },
            pending_toc,
        ],
        pending_records=[
            {
                "toc": pending_toc,
                "relationship": "parallel",
                "nodes": [
                    serialize_title_node(
                        TitleNode(title="App", level=1, printed_page=210)
                    )
                ],
                "skeleton_anchor": serialize_skeleton_anchor(
                    SkeletonAnchor(
                        offset=0,
                        offset_status="ok",
                        match_overrides={("App",): _match("App", 210)},
                        null_page_report=[],
                        bulk_count=1,
                        pruned_count=0,
                        locate_agent="offset_guided_bulk",
                    )
                ),
            }
        ],
    )

    propose_shard_plan(ctx, {})
    plan = ctx.blackboard.shard_plan
    assert plan is not None
    assert [(shard.page_start, shard.page_end) for shard in plan.shards] == [
        (1, 199),
        (200, 350),
    ]


def test_contained_pending_toc_does_not_cut() -> None:
    ctx = _ctx(page_count=250)
    pending_toc = {
        "toc_range": [200, 201],
        "toc_range_unit": "page",
        "toc_with_level": [
            {"heading": "Inner", "level": 1, "page_number": 210},
        ],
    }
    _seed_skeleton(
        ctx,
        nodes=[
            TitleNode(title="Ch1", level=1, printed_page=3),
            TitleNode(title="Ch2", level=1, printed_page=120),
        ],
        overrides={
            ("Ch1",): 3,
            ("Ch2",): 120,
        },
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
            {
                "toc": pending_toc,
                "relationship": "contained",
                "nodes": [
                    serialize_title_node(
                        TitleNode(title="Inner", level=1, printed_page=210)
                    )
                ],
                "skeleton_anchor": serialize_skeleton_anchor(
                    SkeletonAnchor(
                        offset=0,
                        offset_status="ok",
                        match_overrides={("Inner",): _match("Inner", 210)},
                        null_page_report=[],
                        bulk_count=1,
                        pruned_count=0,
                        locate_agent="offset_guided_bulk",
                    )
                ),
            }
        ],
    )

    propose_shard_plan(ctx, {})
    plan = ctx.blackboard.shard_plan
    assert plan is not None
    assert [(shard.page_start, shard.page_end) for shard in plan.shards] == [
        (1, 119),
        (120, 250),
    ]


def test_fat_leaf_uses_blank_page_in_window() -> None:
    ctx = _ctx(page_count=450, blank_pages=[195])
    _seed_skeleton(
        ctx,
        nodes=[TitleNode(title="Only", level=1, printed_page=1)],
        overrides={("Only",): 1},
        hierarchies=[
            {
                "toc_range": [1, 1],
                "toc_range_unit": "page",
                "toc_with_level": [{"heading": "Only", "level": 1, "page_number": 1}],
            }
        ],
    )

    propose_shard_plan(ctx, {})
    plan = ctx.blackboard.shard_plan
    assert plan is not None
    assert plan.shards[0].page_end == 195
    assert plan.shards[0].anchor_type == "blank_separator"
    assert all(shard.page_end - shard.page_start + 1 <= 200 for shard in plan.shards)
    assert plan.validation.valid is True


def test_fat_leaf_without_blank_uses_forced_max_size() -> None:
    ctx = _ctx(page_count=450)
    _seed_skeleton(
        ctx,
        nodes=[TitleNode(title="Only", level=1, printed_page=1)],
        overrides={("Only",): 1},
        hierarchies=[
            {
                "toc_range": [1, 1],
                "toc_range_unit": "page",
                "toc_with_level": [{"heading": "Only", "level": 1, "page_number": 1}],
            }
        ],
    )

    propose_shard_plan(ctx, {})
    plan = ctx.blackboard.shard_plan
    assert plan is not None
    assert [(shard.page_start, shard.page_end, shard.anchor_type) for shard in plan.shards] == [
        (1, 200, "forced_max_size"),
        (201, 400, "forced_max_size"),
        (401, 450, "forced_max_size"),
    ]
    assert plan.validation.valid is True


def test_no_toc_leaves_uses_blank_pages_on_full_document() -> None:
    ctx = _ctx(page_count=450, blank_pages=[195])

    propose_shard_plan(ctx, {})
    plan = ctx.blackboard.shard_plan
    assert plan is not None
    assert plan.shards[0].page_end == 195
    assert plan.shards[0].anchor_type == "blank_separator"
    assert plan.validation.valid is True
