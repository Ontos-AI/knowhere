"""Protocol tests: planner next_action and executor finish ownership."""

from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_agent.budget import BudgetTracker
from app.services.document_agent.executor.react_loop import (
    ReActExecutor,
    _parse_decision,
)
from app.services.document_agent.manifest import (
    DocumentProfile,
    ReflexionDecision,
    ToolContext,
)
from app.services.document_agent.planner.planner import _parse_profile_and_decision
from app.services.document_agent.tools import REGISTRY
from app.services.document_agent.state import AgentBlackboard


def test_planner_verdict_now_falls_through_to_ready_to_shard() -> None:
    raw = json.dumps(
        {
            "is_scanned": True,
            "category": "Feasibility Study Report",
            "routing_category": "generic",
            "category_rationale": "scanned prose",
            "language": "zh",
            "rationale": "scanned PDF not atlas",
            "header_y": None,
            "footer_y": None,
            "next_action": "verdict_now",
            "inspect_pages": [],
            "grep_query": "",
        }
    )
    profile, decision = _parse_profile_and_decision(raw)
    assert profile.is_scanned is True
    assert decision.action == "tool_call"
    assert decision.tool_name == "propose.shard_plan"
    assert decision.verdict is None


def test_planner_ready_to_shard_proposes_shard_plan() -> None:
    raw = json.dumps(
        {
            "is_scanned": False,
            "category": "Report",
            "routing_category": "generic",
            "language": "en",
            "rationale": "enough evidence",
            "next_action": "ready_to_shard",
        }
    )
    _profile, decision = _parse_profile_and_decision(raw)
    assert decision.action == "tool_call"
    assert decision.tool_name == "propose.shard_plan"


def test_planner_inspect_more_maps_to_inspect_pages() -> None:
    raw = json.dumps(
        {
            "is_scanned": False,
            "category": "Report",
            "routing_category": "generic",
            "language": "en",
            "rationale": "need more pages",
            "next_action": "inspect_more",
            "inspect_pages": [3, 8],
        }
    )
    _profile, decision = _parse_profile_and_decision(raw)
    assert decision.tool_name == "inspect.pages"
    assert decision.tool_args["pages"] == [3, 8]


def test_executor_legacy_verdict_now_without_status_becomes_shard() -> None:
    decision = _parse_decision(
        json.dumps(
            {
                "action": "verdict_now",
                "rationale": "classification done",
            }
        )
    )
    assert decision.action == "tool_call"
    assert decision.tool_name == "propose.shard_plan"


def test_executor_legacy_verdict_now_with_abort_status_uses_verdict_tool() -> None:
    decision = _parse_decision(
        json.dumps(
            {
                "action": "verdict_now",
                "rationale": "cannot profile",
                "verdict": {"status": "abort", "rationale": "cannot profile"},
            }
        )
    )
    assert decision.action == "tool_call"
    assert decision.tool_name == "verdict"
    assert decision.tool_args["status"] == "abort"


def _seed_pages(blackboard: AgentBlackboard, page_count: int) -> None:
    from app.services.document_agent.manifest import PageFeature, PageLabel

    blackboard.page_count = page_count
    blackboard.doc_stats = {"page_count": page_count}
    blackboard.page_features = [
        PageFeature(
            page=page,
            raw_text_length=0,
            text_density=0.0,
            image_coverage=1.0,
            image_count=1,
            table_count=0,
            drawings_count=0,
            orientation="portrait",
            width=612.0,
            height=792.0,
            has_asset=True,
            is_blank_like=True,
        )
        for page in range(1, page_count + 1)
    ]
    blackboard.page_labels = [
        PageLabel(page=page, kind="normal", confidence=0.9)
        for page in range(1, page_count + 1)
    ]


def test_executor_initial_ready_to_shard_reaches_success_without_abort() -> None:
    blackboard = AgentBlackboard()
    _seed_pages(blackboard, 4)
    blackboard.document_profile = DocumentProfile(
        is_scanned=True,
        category="Feasibility Study Report",
        routing_category="generic",
        rationale="scanned PDF not atlas",
    )
    from app.services.document_agent.manifest import TocResult

    blackboard.toc_result = TocResult(method="none", notes="no toc")
    ctx = ToolContext(
        pdf_path="/tmp/scanned.pdf",
        job_id="job-scanned",
        blackboard=blackboard,
        budget=BudgetTracker(plan_budget=50_000, visual_budget=80_000),
        trace=None,
        settings={},  # deterministic executor (no LLM)
    )
    initial = ReflexionDecision(
        action="tool_call",
        rationale="ready",
        tool_name="propose.shard_plan",
        tool_args={},
    )
    result = ReActExecutor(
        ctx,
        registry=REGISTRY,
        max_rounds=10,
        initial_decision=initial,
    ).run()
    assert result.verdict.status == "success"
    assert blackboard.shard_plan is not None
    assert len(blackboard.shard_plan.shards) >= 1


def test_executor_empty_initial_tool_falls_through_to_success() -> None:
    """Missing tool_name must coerce to propose.shard_plan, not abort."""
    blackboard = AgentBlackboard()
    _seed_pages(blackboard, 3)
    from app.services.document_agent.manifest import TocResult

    blackboard.toc_result = TocResult(method="none", notes="no toc")
    blackboard.document_profile = DocumentProfile(
        is_scanned=True,
        category="Report",
        routing_category="generic",
    )
    ctx = ToolContext(
        pdf_path="/tmp/scanned.pdf",
        job_id="job-legacy",
        blackboard=blackboard,
        budget=BudgetTracker(plan_budget=50_000, visual_budget=80_000),
        trace=None,
        settings={},
    )
    initial = ReflexionDecision(
        action="tool_call",
        rationale="stale empty decision",
        tool_name=None,
        tool_args={},
    )
    result = ReActExecutor(
        ctx,
        registry=REGISTRY,
        max_rounds=10,
        initial_decision=initial,
    ).run()
    assert result.verdict.status == "success"
    assert blackboard.shard_plan is not None
    assert len(blackboard.shard_plan.shards) == 1
