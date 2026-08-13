"""PROFILE writes skeleton_anchor; C4 and shard plan do not recalibrate."""

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
    ResolvedHierarchyRange,
    TitleMatch,
    TitleNode,
)
from app.services.document_agent.structure.toc_anchoring import (
    classify_toc_relationship,
    run_toc_anchoring,
)
from app.services.document_agent.tools.propose_shard_plan import propose_shard_plan
from app.services.page_memory.skeleton_extractor import extract_section_skeletons


def _ctx(*, page_count: int = 10) -> ToolContext:
    return ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="job-wire",
        blackboard=AgentBlackboard(page_count=page_count),
        budget=BudgetTracker(plan_budget=50_000, visual_budget=80_000),
        trace=None,
        settings={},
    )


def _toc() -> list[dict[str, object]]:
    return [
        {
            "toc_range": [1, 1],
            "toc_range_unit": "page",
            "toc_with_level": [
                {"heading": "Ch1", "level": 1, "page_number": 2},
            ],
        }
    ]


def _node() -> TitleNode:
    return TitleNode(title="Ch1", level=1, printed_page=2, children=[])


def _anchor(*, title: str = "Ch1", page: int = 2) -> SkeletonAnchor:
    return SkeletonAnchor(
        offset=0,
        offset_status="ok",
        match_overrides={
            (title,): TitleMatch(
                page=page,
                confidence=1.0,
                source="anchored",
                matched_line=title,
                score=1.0,
                candidates=[page],
                evidence={},
            )
        },
        null_page_report=[],
        bulk_count=1,
        pruned_count=0,
        locate_agent="offset_guided_bulk",
    )


def _anatomy(*, with_anchor: bool) -> PageAnatomyMap:
    kwargs: dict[str, object] = {
        "job_id": "job-wire",
        "file_path": "/tmp/doc.pdf",
        "page_count": 10,
        "page_features": [
            PageFeature(
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
            for page in range(1, 11)
        ],
        "page_labels": [
            PageLabel(page=page, kind="normal", confidence=1.0)
            for page in range(1, 11)
        ],
        "toc_result": TocResult(method="vlm_batch", toc_pages=[1]),
        "shard_plan": ShardPlan(
            enabled=False,
            reason="not_needed",
            shards=[
                Shard(
                    shard_index=0,
                    page_start=1,
                    page_end=10,
                    page_offset=0,
                    anchor_type="forced_max_size",
                    anchor_evidence="test",
                    confidence=1.0,
                )
            ],
        ),
        "toc_hierarchies": _toc(),
        "toc_page_offset": 0 if with_anchor else None,
    }
    if with_anchor:
        kwargs["skeleton_anchor"] = serialize_skeleton_anchor(_anchor())
        kwargs["skeleton_nodes"] = [serialize_title_node(_node())]
    return PageAnatomyMap(**kwargs)  # type: ignore[arg-type]


def test_profile_toc_anchoring_writes_skeleton_anchor() -> None:
    ctx = _ctx()
    ctx.blackboard.toc_hierarchies = _toc()
    ctx.blackboard.toc_result = TocResult(method="vlm_batch", toc_pages=[1])
    ctx.blackboard.page_full_text_cache = {page: "Ch1" for page in range(1, 11)}

    def fake_anchor_hierarchy(**_kwargs):
        return [_node()], _anchor()

    with patch(
        "app.services.document_agent.structure.toc_anchoring.anchor_hierarchy",
        side_effect=fake_anchor_hierarchy,
    ):
        run_toc_anchoring(ctx)

    assert ctx.blackboard.toc_page_offset == 0
    assert isinstance(ctx.blackboard.skeleton_anchor, dict)
    assert ctx.blackboard.skeleton_anchor["offset"] == 0
    assert ctx.blackboard.skeleton_nodes
    assert ctx.blackboard.skeleton_nodes[0]["title"] == "Ch1"


def test_c4_resolve_does_not_call_calibration() -> None:
    anatomy = _anatomy(with_anchor=True)
    page_texts = {page: "Ch1 body" for page in range(1, 11)}

    def _boom(*_args, **_kwargs):
        raise AssertionError("C4 must not recalibrate")

    with (
        patch(
            "app.services.document_agent.agents.calibration.service.calibrate_offset",
            side_effect=_boom,
        ),
        patch(
            "app.services.document_agent.agents.calibration.orchestrator.anchor_hierarchy",
            side_effect=_boom,
        ),
        patch(
            "app.services.document_agent.agents.calibration.procedure.finalize_calibration_result",
            side_effect=_boom,
        ),
    ):
        skeletons = extract_section_skeletons(
            anatomy=anatomy,
            filename="doc.pdf",
            page_texts=page_texts,
        )

    assert skeletons
    assert skeletons[0].title == "Ch1"
    assert skeletons[0].start_page == 2


def test_shard_plan_reads_offset_and_does_not_calibrate() -> None:
    ctx = _ctx(page_count=250)
    ctx.blackboard.toc_hierarchies = [
        {
            "toc_range": [1, 2],
            "toc_range_unit": "page",
            "toc_with_level": [
                {"heading": "Ch1", "level": 1, "page_number": 3},
                {"heading": "Ch2", "level": 1, "page_number": 120},
            ],
        }
    ]
    ctx.blackboard.toc_page_offset = 0
    ctx.blackboard.skeleton_nodes = [
        serialize_title_node(TitleNode(title="Ch1", level=1, printed_page=3)),
        serialize_title_node(TitleNode(title="Ch2", level=1, printed_page=120)),
    ]
    ctx.blackboard.skeleton_anchor = serialize_skeleton_anchor(
        SkeletonAnchor(
            offset=0,
            offset_status="ok",
            match_overrides={
                ("Ch1",): TitleMatch(
                    page=3,
                    confidence=1.0,
                    source="anchored",
                    matched_line="Ch1",
                    score=1.0,
                    candidates=[3],
                    evidence={},
                ),
                ("Ch2",): TitleMatch(
                    page=120,
                    confidence=1.0,
                    source="anchored",
                    matched_line="Ch2",
                    score=1.0,
                    candidates=[120],
                    evidence={},
                ),
            },
            null_page_report=[],
            bulk_count=2,
            pruned_count=0,
            locate_agent="offset_guided_bulk",
        )
    )
    ctx.blackboard.toc_result = TocResult(method="vlm_batch")
    ctx.blackboard.doc_stats = {"page_count": 250}
    ctx.settings["shard_threshold"] = 200
    ctx.settings["max_pages_per_shard"] = 200

    def _boom(*_args, **_kwargs):
        raise AssertionError("shard plan must not recalibrate")

    with patch(
        "app.services.document_agent.agents.calibration.service.calibrate_offset",
        side_effect=_boom,
    ):
        result = propose_shard_plan(ctx, {})

    assert result.status == "ok"
    assert ctx.blackboard.toc_page_offset == 0
    assert ctx.blackboard.shard_plan is not None


def _pending_tocs() -> list[dict[str, object]]:
    return [
        {
            "toc_range": [1, 1],
            "toc_range_unit": "page",
            "toc_with_level": [
                {"heading": "Ch1", "level": 1, "page_number": 2},
            ],
        },
        {
            "toc_range": [20, 21],
            "toc_range_unit": "page",
            "toc_with_level": [
                {"heading": "App", "level": 1, "page_number": 22},
            ],
        },
    ]


def test_profile_classifies_pending_toc_before_finalize() -> None:
    ctx = _ctx(page_count=30)
    ctx.blackboard.toc_hierarchies = _pending_tocs()
    ctx.blackboard.toc_result = TocResult(method="vlm_batch", toc_pages=[1, 20, 21])
    ctx.blackboard.page_full_text_cache = {page: "body" for page in range(1, 31)}
    pending_node = TitleNode(title="App", level=1, printed_page=22, children=[])
    finalize_calls: list[object] = []

    def fake_finalize(**kwargs):
        finalize_calls.append(kwargs["nodes"])
        return [pending_node], _anchor(title="App", page=22), True

    with (
        patch(
            "app.services.document_agent.structure.toc_anchoring.anchor_hierarchy",
            return_value=([_node()], _anchor()),
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring.resolve_hierarchy_page_ranges",
            return_value=[
                ResolvedHierarchyRange(
                    title="Ch1",
                    level=1,
                    start_page=2,
                    end_page=19,
                    path_titles=("Ch1",),
                    match=None,
                )
            ],
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
            "app.services.document_agent.structure.toc_anchoring.finalize_calibration_result",
            side_effect=fake_finalize,
        ),
    ):
        run_toc_anchoring(ctx)

    records = ctx.blackboard.pending_skeleton_anchors
    assert len(records) == 1
    assert records[0]["relationship"] == "parallel"
    assert finalize_calls
    assert records[0]["nodes"][0]["title"] == "App"


def test_profile_skips_finalize_for_unresolvable_pending_toc() -> None:
    ctx = _ctx(page_count=30)
    hierarchies = _pending_tocs()
    hierarchies[1]["toc_with_level"] = [{"heading": "App", "level": 1}]
    ctx.blackboard.toc_hierarchies = hierarchies
    ctx.blackboard.toc_result = TocResult(method="vlm_batch", toc_pages=[1, 20, 21])
    ctx.blackboard.page_full_text_cache = {page: "body" for page in range(1, 31)}

    def _boom(*_args, **_kwargs):
        raise AssertionError("unresolvable pending TOC must not finalize")

    with (
        patch(
            "app.services.document_agent.structure.toc_anchoring.anchor_hierarchy",
            return_value=([_node()], _anchor()),
        ),
        patch(
            "app.services.document_agent.structure.toc_anchoring.resolve_hierarchy_page_ranges",
            return_value=[
                ResolvedHierarchyRange(
                    title="Ch1",
                    level=1,
                    start_page=2,
                    end_page=19,
                    path_titles=("Ch1",),
                    match=None,
                )
            ],
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
            "app.services.document_agent.structure.toc_anchoring.finalize_calibration_result",
            side_effect=_boom,
        ),
    ):
        run_toc_anchoring(ctx)

    records = ctx.blackboard.pending_skeleton_anchors
    assert len(records) == 1
    assert records[0]["relationship"] == "unresolvable"
    assert "nodes" not in records[0]
    assert "skeleton_anchor" not in records[0]


def test_c4_uses_persisted_pending_relationship_and_does_not_classify() -> None:
    pending_toc = _pending_tocs()[1]
    anatomy = _anatomy(with_anchor=True)
    anatomy.page_count = 30
    anatomy.page_features = [
        PageFeature(
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
        for page in range(1, 31)
    ]
    anatomy.page_labels = [
        PageLabel(page=page, kind="normal", confidence=1.0)
        for page in range(1, 31)
    ]
    anatomy.toc_hierarchies = _pending_tocs()
    anatomy.pending_skeleton_anchors = [
        {
            "toc": pending_toc,
            "relationship": "parallel",
            "nodes": [
                serialize_title_node(
                    TitleNode(title="App", level=1, printed_page=22, children=[])
                )
            ],
            "skeleton_anchor": serialize_skeleton_anchor(_anchor(title="App", page=22)),
        }
    ]
    page_texts = {page: "Ch1 body" for page in range(1, 31)}
    page_texts[22] = "App"

    def _boom(*_args, **_kwargs):
        raise AssertionError("C4 must not classify pending TOC")

    with patch(
        "app.services.document_agent.structure.toc_anchoring.classify_toc_relationship",
        side_effect=_boom,
    ):
        skeletons = extract_section_skeletons(
            anatomy=anatomy,
            filename="doc.pdf",
            page_texts=page_texts,
        )

    titles = [skeleton.title for skeleton in skeletons]
    assert "Ch1" in titles
    assert "App" in titles
    app = next(skeleton for skeleton in skeletons if skeleton.title == "App")
    assert app.evidence["page_locate_summary"]["toc_relationship"] == "parallel"


def test_classify_toc_relationship_is_not_on_c4_module() -> None:
    import app.services.page_memory.skeleton_extractor as skeleton_extractor

    assert not hasattr(skeleton_extractor, "_classify_toc_relationship")
    assert callable(classify_toc_relationship)


def test_toc_anchoring_requires_page_text_cache() -> None:
    ctx = _ctx()
    ctx.blackboard.toc_hierarchies = _toc()
    ctx.blackboard.toc_result = TocResult(method="vlm_batch", toc_pages=[1])

    try:
        run_toc_anchoring(ctx)
        raise AssertionError("expected missing cache to raise")
    except ValueError as exc:
        assert "page_full_text_cache" in str(exc)


def test_page_memory_and_toc_do_not_extract_page_texts() -> None:
    import inspect

    from app.services.document_agent.structure import toc_anchoring
    from app.services.document_agent.tools import find_toc_anchor_pages, grep_text
    from app.services.page_memory import memory_service, page_renderer, skeleton_extractor

    for module in (
        memory_service,
        page_renderer,
        skeleton_extractor,
        toc_anchoring,
        find_toc_anchor_pages,
        grep_text,
    ):
        assert "read_page_texts(" not in inspect.getsource(module)
