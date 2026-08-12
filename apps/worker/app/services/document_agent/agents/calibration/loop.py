"""ReAct loop for the calibration SubAgent."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from loguru import logger

from app.services.document_agent.agents.calibration.tools import (
    build_calibration_registry,
    strip_toc_links,
)
from app.services.document_agent.agents.calibration.procedure import (
    build_calibration_payload,
    finalize_calibration_result,
)
from app.services.document_agent.agents.calibration.types import (
    CalibrationResult,
    calibration_result_from_dict,
)
from app.services.document_agent.budget import BudgetTracker
from app.services.document_agent.manifest import ToolContext, ToolResult
from app.services.document_agent.state import AgentBlackboard
from app.services.document_agent.structure.structure_anchoring import (
    deserialize_skeleton_anchor,
    serialize_skeleton_anchor,
)
from shared.utils.token_estimate import estimate_tokens

_SKILL_PATH = Path(__file__).resolve().parent / "SKILL.md"

_DECISION_INSTRUCTIONS = """
You are the calibration SubAgent. Follow the Skill strictly.
Each turn return a JSON object with keys:
  action: "tool_call"
  rationale: string
  tool_name: one of the available tools
  tool_args: object
Your job is Phase 1 only: partition regimes and find candidate offsets via
inspect.pages, then call calibration.submit.
Phase 2 (tail verify, binary search, small-step recalibrate) runs automatically
after submit. Do not use a fixed post-TOC page window.
Include the word json in your response.
""".strip()


def _load_skill() -> str:
    return _SKILL_PATH.read_text(encoding="utf-8")


def _parse_decision(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        return {"tool_name": None, "tool_args": {}, "rationale": "invalid decision"}
    tool_name = data.get("tool_name") or data.get("name") or data.get("tool")
    tool_args = data.get("tool_args") or data.get("arguments") or data.get("args") or {}
    if not isinstance(tool_args, dict):
        tool_args = {}
    return {
        "tool_name": tool_name,
        "tool_args": dict(tool_args),
        "rationale": str(data.get("rationale") or ""),
    }


def _attach_history(result: CalibrationResult, history: list[dict[str, Any]]) -> CalibrationResult:
    result.history_tail = history[-12:]
    return result


def _toc_region_payload(
    hierarchies: list[dict[str, Any]],
    region_index: int,
) -> dict[str, Any]:
    if region_index < 0 or region_index >= len(hierarchies):
        raise IndexError(f"region_index out of range: {region_index}")
    region = hierarchies[region_index]
    entries = region.get("toc_with_level") if isinstance(region, dict) else None
    return {
        "region_index": region_index,
        "toc_range": region.get("toc_range") if isinstance(region, dict) else None,
        "entries": entries if isinstance(entries, list) else [],
    }


def run_calibration_phase1(
    *,
    ctx: ToolContext,
    toc_hierarchies: list[dict[str, Any]],
    region_index: int = 0,
    page_count: int | None = None,
    no_links: bool = False,
    max_rounds: int = 16,
    inspect_page_cap: int = 5,
    inspect_page_budget: int = 24,
) -> CalibrationResult:
    """Agent Phase-1 only: partition regimes + candidate offsets, then submit.

    Reuses the caller's ``ToolContext`` (budget / pdf / settings). Does **not**
    run production Phase-2 bulk anchoring.
    """
    hierarchies = list(toc_hierarchies or [])
    if no_links:
        hierarchies = strip_toc_links(hierarchies)
    if not hierarchies:
        return CalibrationResult(status="failed", notes="toc_hierarchies empty")
    region_payload = _toc_region_payload(hierarchies, region_index)
    resolved_page_count = int(
        page_count or ctx.blackboard.page_count or 0
    )
    if resolved_page_count:
        ctx.blackboard.page_count = resolved_page_count

    ctx.settings.setdefault("inspect_page_cap", inspect_page_cap)
    ctx.settings.setdefault("inspect_page_budget", inspect_page_budget)

    blackboard = ctx.blackboard
    blackboard.global_signals["calibration_region_index"] = region_index
    blackboard.global_signals["calibration_tool_calls"] = 0
    blackboard.global_signals["calibration_inspect_pages_used"] = int(
        blackboard.global_signals.get("calibration_inspect_pages_used") or 0
    )
    blackboard.global_signals["calibration_done"] = False
    blackboard.global_signals.pop("calibration_result", None)

    registry = build_calibration_registry()
    skill = _load_skill()
    history: list[dict[str, Any]] = []

    for round_index in range(max_rounds):
        available = registry.openai_specs(blackboard)
        payload = {
            "skill": skill,
            "page_count": resolved_page_count,
            "no_links": no_links,
            "budgets": {
                "max_rounds": max_rounds,
                "round_index": round_index,
                "rounds_remaining": max_rounds - round_index,
                "inspect_page_cap_per_call": int(
                    ctx.settings.get("inspect_page_cap") or inspect_page_cap
                ),
                "inspect_page_budget_total": int(
                    ctx.settings.get("inspect_page_budget") or inspect_page_budget
                ),
                "inspect_pages_used": blackboard.global_signals.get(
                    "calibration_inspect_pages_used"
                ),
            },
            "toc_region": region_payload,
            "history_tail": history[-8:],
            "available_tools": available,
        }
        prompt = _DECISION_INSTRUCTIONS + "\nPayload:\n" + json.dumps(
            payload, ensure_ascii=False
        )
        model = ctx.settings.get("model") or ctx.settings.get("vlm_model")
        if not model:
            return _attach_history(
                CalibrationResult(
                    status="failed",
                    notes="planner model missing",
                    region_index=region_index,
                ),
                history,
            )

        est = estimate_tokens(prompt)
        if not ctx.budget.try_reserve("plan", est):
            return _attach_history(
                CalibrationResult(
                    status="failed",
                    notes="planner budget exhausted",
                    region_index=region_index,
                    tool_calls=int(
                        blackboard.global_signals.get("calibration_tool_calls") or 0
                    ),
                ),
                history,
            )

        try:
            from shared.services.ai.llm_overrides import get_text_client

            client, model = get_text_client(requested_model=str(model))
            raw, usage = client.chat_completion_with_usage(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.0,
                max_tokens=2500,
                response_format={"type": "json_object"},
                usage_task="calibration.react_loop",
            )
            ctx.budget.commit("plan", actual=usage.get("total_tokens", est), est=est)
            decision = _parse_decision(raw)
        except Exception as exc:
            ctx.budget.refund("plan", est=est)
            logger.warning("[calibration] decision failed round={}: {}", round_index, exc)
            return _attach_history(
                CalibrationResult(
                    status="failed",
                    notes=f"decision failed: {exc}",
                    region_index=region_index,
                    tool_calls=int(
                        blackboard.global_signals.get("calibration_tool_calls") or 0
                    ),
                ),
                history,
            )

        tool_name = str(decision.get("tool_name") or "").strip()
        tool_args = dict(decision.get("tool_args") or {})
        if not tool_name:
            history.append(
                {
                    "round": round_index,
                    "error": "missing tool_name",
                    "decision": decision,
                }
            )
            continue

        tool_result: ToolResult = registry.dispatch(tool_name, ctx, tool_args)
        blackboard.global_signals["calibration_tool_calls"] = (
            int(blackboard.global_signals.get("calibration_tool_calls") or 0) + 1
        )
        history.append(
            {
                "round": round_index,
                "rationale": decision.get("rationale"),
                "tool_name": tool_name,
                "tool_args": tool_args,
                "tool_status": tool_result.status,
                "tool_payload": tool_result.output_summary
                if tool_result.status == "ok"
                else tool_result.payload,
                "tool_error": tool_result.error,
            }
        )
        logger.info(
            "[calibration] region={} round={} tool={} status={}",
            region_index,
            round_index,
            tool_name,
            tool_result.status,
        )

        if blackboard.global_signals.get("calibration_done"):
            raw_result = blackboard.global_signals.get("calibration_result") or {}
            if isinstance(raw_result, dict):
                parsed = calibration_result_from_dict(raw_result)
                parsed.region_index = region_index
                parsed.tool_calls = int(
                    blackboard.global_signals.get("calibration_tool_calls") or 0
                )
                return _attach_history(parsed, history)

    return _attach_history(
        CalibrationResult(
            status="failed",
            notes="max rounds reached without calibration.submit",
            region_index=region_index,
            tool_calls=int(blackboard.global_signals.get("calibration_tool_calls") or 0),
        ),
        history,
    )


def run_calibration_agent(
    *,
    pdf_path: str,
    page_count: int,
    toc_hierarchies: list[dict[str, Any]],
    region_index: int = 0,
    output_dir: str,
    vlm_model: str | None = None,
    planner_model: str | None = None,
    no_links: bool = False,
    max_rounds: int = 16,
    inspect_page_cap: int = 5,
    inspect_page_budget: int = 24,
    budget: BudgetTracker | None = None,
    page_texts: dict[int, str] | None = None,
    body_pages: list[int] | None = None,
) -> tuple[CalibrationResult, dict[str, Any]]:
    """Debug/full path: Phase-1 agent + production Phase-2 finalize."""
    hierarchies = list(toc_hierarchies or [])
    if no_links:
        hierarchies = strip_toc_links(hierarchies)
    region_hierarchies = [hierarchies[region_index]] if hierarchies else []
    region_payload = _toc_region_payload(hierarchies, region_index) if hierarchies else {
        "entries": []
    }

    blackboard = AgentBlackboard()
    blackboard.page_count = page_count
    if page_texts:
        blackboard.page_full_text_cache = dict(page_texts)

    ctx = ToolContext(
        pdf_path=pdf_path,
        job_id=f"calibration-region-{region_index}",
        blackboard=blackboard,
        budget=budget
        or BudgetTracker(
            plan_budget=int(os.environ.get("PARSE_AGENT_PLAN_BUDGET", "50000")),
            visual_budget=int(os.environ.get("PARSE_AGENT_VISUAL_BUDGET", "80000")),
        ),
        trace=None,
        output_dir=output_dir,
        settings={
            "vlm_model": vlm_model or "",
            "model": planner_model or vlm_model or "",
            "inspect_page_cap": inspect_page_cap,
            "inspect_page_budget": inspect_page_budget,
        },
    )

    phase1 = run_calibration_phase1(
        ctx=ctx,
        toc_hierarchies=hierarchies,
        region_index=region_index,
        page_count=page_count,
        no_links=False,  # already stripped above when requested
        max_rounds=max_rounds,
        inspect_page_cap=inspect_page_cap,
        inspect_page_budget=inspect_page_budget,
    )
    if phase1.status == "failed" and not phase1.regimes:
        return phase1, {}

    anchor, finalized = finalize_calibration_result(
        result=phase1,
        entries=list(region_payload.get("entries") or []),
        toc_hierarchies=region_hierarchies,
        ctx=ctx,
        page_count=page_count,
        page_texts=page_texts,
        body_pages=body_pages,
    )
    finalized.history_tail = list(phase1.history_tail)
    return finalized, serialize_skeleton_anchor(anchor)


def run_calibration_for_all_regions(
    *,
    pdf_path: str,
    page_count: int,
    toc_hierarchies: list[dict[str, Any]],
    output_dir: str,
    vlm_model: str | None = None,
    planner_model: str | None = None,
    no_links: bool = False,
    max_rounds: int = 16,
    budget: BudgetTracker | None = None,
    page_texts: dict[int, str] | None = None,
    body_pages: list[int] | None = None,
) -> dict[str, Any]:
    """Calibrate each TOC region; return production SkeletonAnchor-shaped payload."""
    hierarchies = list(toc_hierarchies or [])
    if not hierarchies:
        return {
            "offset": None,
            "offset_status": "failed",
            "match_overrides": {},
            "null_page_report": [],
            "bulk_count": 0,
            "pruned_count": 0,
            "locate_agent": "offset_only",
            "status": "failed",
            "regimes": [],
            "regions": [],
            "notes": "toc_hierarchies empty",
            "tool_calls": 0,
            "no_links": no_links,
        }

    region_results: list[dict[str, Any]] = []
    all_regimes: list[dict[str, Any]] = []
    tool_calls = 0
    primary_anchor: dict[str, Any] | None = None
    primary_result: CalibrationResult | None = None

    for idx in range(len(hierarchies)):
        t0 = time.time()
        result, anchor_dict = run_calibration_agent(
            pdf_path=pdf_path,
            page_count=page_count,
            toc_hierarchies=hierarchies,
            region_index=idx,
            output_dir=output_dir,
            vlm_model=vlm_model,
            planner_model=planner_model,
            no_links=no_links,
            max_rounds=max_rounds,
            budget=budget,
            page_texts=page_texts,
            body_pages=body_pages,
        )
        payload = result.to_dict()
        payload["elapsed_s"] = round(time.time() - t0, 2)
        payload["skeleton_anchor"] = anchor_dict
        region_results.append(payload)
        tool_calls += int(result.tool_calls or 0)
        for regime in payload.get("regimes") or []:
            if isinstance(regime, dict):
                tagged = dict(regime)
                tagged["region_index"] = idx
                all_regimes.append(tagged)
        if primary_anchor is None and anchor_dict.get("offset") is not None:
            primary_anchor = anchor_dict
            primary_result = result

    if primary_anchor is None:
        primary_anchor = {
            "offset": None,
            "offset_status": "failed",
            "match_overrides": {},
            "null_page_report": [],
            "bulk_count": 0,
            "pruned_count": 0,
            "locate_agent": "offset_only",
        }
    if primary_result is None:
        primary_result = CalibrationResult(
            status="failed", notes="no region produced offset"
        )

    anchor = deserialize_skeleton_anchor(primary_anchor)
    status = (
        "ok"
        if anchor.offset is not None and int(anchor.bulk_count or 0) > 0
        else "failed"
    )
    merged = build_calibration_payload(
        anchor=anchor,
        result=CalibrationResult(
            status=status,
            regimes=[],
            offset=anchor.offset,
            offset_status=anchor.offset_status,
            tool_calls=tool_calls,
            notes=primary_result.notes,
        ),
        no_links=no_links,
        region_payloads=region_results,
        tool_calls=tool_calls,
    )
    merged["status"] = status
    merged["regimes"] = all_regimes
    merged["regions"] = region_results
    return merged
