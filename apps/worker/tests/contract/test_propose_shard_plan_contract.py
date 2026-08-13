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
            "min_pages_per_shard": 20,
        },
    )
    ctx.blackboard.doc_stats = {"page_count": page_count}
    ctx.blackboard.toc_result = TocResult(method="vlm_batch")
    ctx.blackboard.page_features = [
        _feature(page, blank=page in blanks) for page in range(1, page_count + 1)
    ]
    return ctx


def test_leaf_plan_cuts_at_finest_toc_boundary() -> None:
    ctx = _ctx(page_count=250)
    ctx.blackboard.toc_page_offset = 0
    ctx.blackboard.toc_hierarchies = [
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
    ]

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


def test_fat_leaf_uses_blank_page_in_window() -> None:
    ctx = _ctx(page_count=450, blank_pages=[195])
    ctx.blackboard.toc_page_offset = 0
    ctx.blackboard.toc_hierarchies = [
        {
            "toc_range": [1, 1],
            "toc_range_unit": "page",
            "toc_with_level": [{"heading": "Only", "level": 1, "page_number": 1}],
        }
    ]

    propose_shard_plan(ctx, {})
    plan = ctx.blackboard.shard_plan
    assert plan is not None
    assert plan.shards[0].page_end == 195
    assert plan.shards[0].anchor_type == "blank_separator"
    assert all(shard.page_end - shard.page_start + 1 <= 200 for shard in plan.shards)
    assert plan.validation.valid is True


def test_fat_leaf_without_blank_uses_forced_max_size() -> None:
    ctx = _ctx(page_count=450)
    ctx.blackboard.toc_page_offset = 0
    ctx.blackboard.toc_hierarchies = [
        {
            "toc_range": [1, 1],
            "toc_range_unit": "page",
            "toc_with_level": [{"heading": "Only", "level": 1, "page_number": 1}],
        }
    ]

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
