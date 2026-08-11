"""Map map-nav ``AgentStep`` list → public ``decision_trace``.

Replacement for the former stub: one DecisionTraceStep per stamped AgentStep
(plus a terminal row), shaped for PLANNER / HARVEST / CONTROL.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from shared.services.retrieval.nav_config import (
    MAPNAV_EVIDENCE_CHARS,
    MAPNAV_MODEL,
    MAPNAV_TRACE_RAW_CHARS,
)
from shared.services.retrieval.trace.types import DecisionTraceStep


def _detail(step: Any) -> dict[str, Any]:
    raw = getattr(step, "detail", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _clip_raw(text: Any) -> str:
    s = str(text or "")
    limit = int(MAPNAV_TRACE_RAW_CHARS)
    if limit > 0 and len(s) > limit:
        return s[:limit]
    return s


def _budget_from_detail(
    detail: dict[str, Any],
    *,
    evidence_chars_used: int,
    evidence_char_budget: int,
) -> dict[str, Any]:
    token_limit = int(detail.get("token_limit") or 0)
    used_total = int(detail.get("tokens_used_total") or 0)
    used_delta = int(detail.get("tokens_used_delta") or 0)
    remaining = max(0, token_limit - used_total) if token_limit > 0 else None
    out: dict[str, Any] = {
        "token_limit": token_limit,
        "tokens_used_total": used_total,
        "tokens_used_delta": used_delta,
        "evidence_chars_used": int(evidence_chars_used),
        "evidence_char_budget": int(evidence_char_budget),
    }
    if remaining is not None:
        out["remaining"] = remaining
    return out


def _elapsed_ms(detail: dict[str, Any]) -> Optional[int]:
    if "elapsed_ms" in detail and detail.get("elapsed_ms") is not None:
        try:
            return int(detail.get("elapsed_ms") or 0)
        except (TypeError, ValueError):
            return None
    if "seconds" in detail and detail.get("seconds") is not None:
        try:
            return int(round(float(detail.get("seconds") or 0.0) * 1000.0))
        except (TypeError, ValueError):
            return None
    return None


def _map_one(
    *,
    action: str,
    detail: dict[str, Any],
    step_index: int,
    parent_step_index: Optional[int],
    evidence_chars_used: int,
    evidence_char_budget: int,
) -> Optional[DecisionTraceStep]:
    budget = _budget_from_detail(
        detail,
        evidence_chars_used=evidence_chars_used,
        evidence_char_budget=evidence_char_budget,
    )
    elapsed = _elapsed_ms(detail)
    scope = detail.get("scope")
    if scope is not None:
        scope = str(scope)

    if action == "query_plan":
        plan = detail.get("plan") if isinstance(detail.get("plan"), dict) else {}
        return DecisionTraceStep(
            step_index=step_index,
            agent="planner",
            phase="plan",
            parent_step_index=parent_step_index,
            scope=scope,
            observation={
                "projection_chars": detail.get("projection_chars"),
                "hit_section_ids": detail.get("hit_section_ids") or [],
                "planning_map_char_limit": detail.get("planning_map_char_limit"),
                "llm_raw": _clip_raw(plan.get("raw") or detail.get("llm_raw") or detail.get("raw")),
            },
            decision={
                "action": "plan_query",
                "reason": detail.get("reason") or plan.get("reason") or "",
            },
            result={
                "status": "fallback" if detail.get("fallback") else "ok",
                "subgoals": plan.get("subgoals") or [],
                "coverage_checklist": plan.get("coverage_checklist") or [],
                "map_coverage": plan.get("map_coverage"),
                "n_subgoals": detail.get("n_subgoals"),
                "reason": detail.get("reason") or plan.get("reason") or "",
            },
            budget=budget,
            elapsed_ms=elapsed,
        )

    if action == "plan_orchestrate":
        return DecisionTraceStep(
            step_index=step_index,
            agent="planner",
            phase="plan",
            parent_step_index=parent_step_index,
            scope=scope,
            observation={},
            decision={"action": "orchestrate"},
            result={
                "status": "ok",
                "n_waves": detail.get("n_waves")
                or len(detail.get("waves") or []),
                "satisfied": detail.get("satisfied"),
                "attempted": detail.get("attempted"),
                "dropped": detail.get("dropped"),
                "waves": detail.get("waves"),
                "fallback_navigate": detail.get("fallback_navigate"),
            },
            budget=budget,
            elapsed_ms=elapsed,
        )

    if action == "plan_wave":
        return DecisionTraceStep(
            step_index=step_index,
            agent="orchestrator",
            phase="plan_wave",
            parent_step_index=parent_step_index,
            scope=scope,
            observation={},
            decision={
                "action": "plan_wave",
                "wave": detail.get("wave"),
                "subgoal_ids": detail.get("subgoal_ids")
                or detail.get("ready")
                or [],
            },
            result={
                "status": "ok",
                "subgoals": detail.get("subgoals") or detail.get("results"),
                "chars_used": detail.get("chars_used"),
                "plan_control": detail.get("plan_control"),
            },
            budget=budget,
            elapsed_ms=elapsed,
        )

    if action == "harvest":
        return DecisionTraceStep(
            step_index=step_index,
            agent="navigator",
            phase="harvest",
            parent_step_index=parent_step_index,
            scope=scope,
            observation={
                "visible_section_ids": detail.get("visible_section_ids") or [],
                "legal_actions_preview": detail.get("legal_actions_preview") or [],
                "projection_chars": detail.get("projection_chars"),
                "scope": scope,
                "depth": detail.get("depth"),
                "llm_raw": _clip_raw(detail.get("raw") or detail.get("llm_raw")),
            },
            decision={
                "action": "harvest",
                "collect_ids": detail.get("collect_ids") or [],
                "dispatch_ids": detail.get("dispatch_ids") or [],
                "search_assets": detail.get("search_assets") or [],
                "confidence": detail.get("confidence"),
                "reason": detail.get("reason") or "",
            },
            result={
                "status": "ok",
                "subgoal_id": detail.get("subgoal_id"),
                "collect_section_ids": detail.get("collect_section_ids") or [],
                "n_added": detail.get("n_added"),
                "fallback_used": detail.get("fallback_used"),
                "model": detail.get("model") or MAPNAV_MODEL,
            },
            budget=budget,
            elapsed_ms=elapsed,
        )

    if action == "search_assets":
        return DecisionTraceStep(
            step_index=step_index,
            agent="asset_inspector",
            phase="asset_search",
            parent_step_index=parent_step_index,
            scope=scope,
            observation={
                "candidates": detail.get("candidates")
                or detail.get("requests")
                or [],
            },
            decision={
                "action": "search_assets",
                "asset_ids": detail.get("asset_ids") or [],
            },
            result={
                "status": "ok",
                "n_added": detail.get("n_added"),
                "subgoal_id": detail.get("subgoal_id"),
                "requests": detail.get("requests"),
            },
            budget=budget,
            elapsed_ms=elapsed,
        )

    if action in {
        "nav_collect",
        "nav_dispatch",
        "nav_finish",
        "nav_dispatch_skipped",
    } or action.startswith("nav_"):
        kind = {
            "nav_collect": "COLLECT",
            "nav_dispatch": "DISPATCH",
            "nav_finish": "FINISH",
            "nav_dispatch_skipped": "SKIP",
        }.get(action)
        if kind is None and action.startswith("nav_"):
            kind = action[4:].upper() or "STEP"
        return DecisionTraceStep(
            step_index=step_index,
            agent="navigator",
            phase="navigate",
            parent_step_index=parent_step_index,
            scope=scope,
            observation={
                "legal_actions_preview": detail.get("legal_actions_preview") or [],
                "projection_chars": detail.get("projection_chars"),
                "n_legal_actions": detail.get("n_legal_actions"),
                "llm_raw": _clip_raw(detail.get("llm_raw") or detail.get("raw")),
            },
            decision={
                "action": kind,
                "action_id": detail.get("action_id"),
                "ids": detail.get("ids") or detail.get("section_ids"),
                "reason": detail.get("reason") or "",
            },
            result={
                "status": "ok",
                "collect_section_ids": detail.get("collect_section_ids") or [],
                "kind": detail.get("kind") or kind,
            },
            budget=budget,
            elapsed_ms=elapsed,
        )

    if action == "plan_control":
        return DecisionTraceStep(
            step_index=step_index,
            agent="controller",
            phase="plan_control",
            parent_step_index=parent_step_index,
            scope=scope,
            observation={
                "coverage": detail.get("coverage")
                or detail.get("checklist")
                or detail.get("subgoals"),
            },
            decision={
                "action": detail.get("global") or "continue",
                "subgoals": detail.get("subgoals") or {},
                "reason": detail.get("reason") or "",
            },
            result={
                "status": "ok",
                "global": detail.get("global"),
                "reason": detail.get("reason") or "",
            },
            budget=budget,
            elapsed_ms=elapsed,
        )

    if action == "replan":
        plan = detail.get("plan") if isinstance(detail.get("plan"), dict) else {}
        return DecisionTraceStep(
            step_index=step_index,
            agent="controller",
            phase="plan_control",
            parent_step_index=parent_step_index,
            scope=scope,
            observation={},
            decision={
                "action": "replan",
                "reason": detail.get("reason") or "",
            },
            result={
                "status": "ok",
                "plan": plan or detail.get("plan"),
                "n_subgoals": detail.get("n_subgoals"),
            },
            budget=budget,
            elapsed_ms=elapsed,
        )

    if action == "slot_extract":
        return DecisionTraceStep(
            step_index=step_index,
            agent="controller",
            phase="plan_control",
            parent_step_index=parent_step_index,
            scope=scope,
            observation={
                "slots_requested": detail.get("slots_requested") or [],
                "llm_raw": _clip_raw(detail.get("raw") or detail.get("llm_raw")),
            },
            decision={"action": "slot_extract"},
            result={
                "status": "ok",
                "subgoal_id": detail.get("subgoal_id"),
                "slots": detail.get("slots") or detail.get("extracted") or {},
            },
            budget=budget,
            elapsed_ms=elapsed,
        )

    if action == "compose_answer":
        return DecisionTraceStep(
            step_index=step_index,
            agent="composer",
            phase="compose",
            parent_step_index=parent_step_index,
            scope=scope,
            observation={},
            decision={"action": "compose_answer"},
            result={"status": "ok", **{
                k: detail.get(k)
                for k in ("chars", "model", "truncated")
                if k in detail
            }},
            budget=budget,
            elapsed_ms=elapsed,
        )

    return None


def build_decision_trace(
    episode: Any,
    *,
    evidence_char_budget: int = MAPNAV_EVIDENCE_CHARS,
    n_refs: int = 0,
) -> list[DecisionTraceStep]:
    """Map episode AgentSteps → DecisionTraceStep list (incl. terminal)."""
    evidence_chars_used = int(getattr(episode, "evidence_chars_actual", 0) or 0)
    steps_in: Sequence[Any] = list(getattr(episode, "steps", None) or ())
    out: list[DecisionTraceStep] = []
    harvest_parent_by_depth: dict[int, int] = {}
    layer_counts = {"planner": 0, "harvest": 0, "control": 0, "navigate": 0}

    for raw_step in steps_in:
        action = str(getattr(raw_step, "action", "") or "").strip()
        detail = _detail(raw_step)
        parent: Optional[int] = None
        if action == "harvest":
            try:
                depth = int(detail.get("depth") or 0)
            except (TypeError, ValueError):
                depth = 0
            if depth > 0:
                parent = harvest_parent_by_depth.get(depth - 1)
        mapped = _map_one(
            action=action,
            detail=detail,
            step_index=len(out),
            parent_step_index=parent,
            evidence_chars_used=evidence_chars_used,
            evidence_char_budget=evidence_char_budget,
        )
        if mapped is None:
            continue
        out.append(mapped)
        if action == "harvest":
            try:
                depth = int(detail.get("depth") or 0)
            except (TypeError, ValueError):
                depth = 0
            harvest_parent_by_depth[depth] = mapped.step_index
        if mapped.phase == "plan":
            layer_counts["planner"] += 1
        elif mapped.phase in {"harvest", "plan_wave", "asset_search"}:
            layer_counts["harvest"] += 1
        elif mapped.phase == "plan_control":
            layer_counts["control"] += 1
        elif mapped.phase == "navigate":
            layer_counts["navigate"] += 1

    stop = str(getattr(episode, "stop_reason", "") or "completed")
    last_budget = out[-1].budget if out and out[-1].budget else {
        "token_limit": 0,
        "tokens_used_total": 0,
        "tokens_used_delta": 0,
        "evidence_chars_used": evidence_chars_used,
        "evidence_char_budget": int(evidence_char_budget),
    }
    out.append(
        DecisionTraceStep(
            step_index=len(out),
            agent="retrieval_agent",
            phase="terminal",
            scope="mapnav",
            observation={
                "router_used": "mapnav",
                "n_nav_steps": len(steps_in),
                "evidence_chars": evidence_chars_used,
            },
            decision={"action": "complete", "args": {}, "reason": stop},
            result={
                "status": "ok",
                "stop_reason": stop,
                "evidence_chars": evidence_chars_used,
                "n_refs": int(n_refs),
                "layer_llm_steps": dict(layer_counts),
            },
            budget=dict(last_budget),
            elapsed_ms=None,
        )
    )
    return out


def episode_token_count(episode: Any) -> int:
    """Max stamped tokens_used_total on AgentSteps (episode counter is gone after return)."""
    best = 0
    for step in getattr(episode, "steps", None) or ():
        detail = _detail(step)
        try:
            best = max(best, int(detail.get("tokens_used_total") or 0))
        except (TypeError, ValueError):
            continue
    return best


def episode_workflow_plan(episode: Any) -> Optional[dict[str, Any]]:
    """Nav plan dict for ``retrieval_runs.workflow_plan``."""
    for step in getattr(episode, "steps", None) or ():
        if str(getattr(step, "action", "") or "") != "query_plan":
            continue
        detail = _detail(step)
        plan = detail.get("plan")
        if isinstance(plan, dict):
            return plan
    return None


def episode_selected_paths(
    episode: Any,
    refs: Sequence[dict[str, Any]],
) -> list[str]:
    """Section paths from resolved refs (DB-original paths on refs)."""
    paths: list[str] = []
    seen: set[str] = set()
    for ref in refs or ():
        path = str(ref.get("section_path") or "").strip()
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def episode_selected_doc_ids(refs: Sequence[dict[str, Any]]) -> list[str]:
    docs: list[str] = []
    seen: set[str] = set()
    for ref in refs or ():
        doc = str(ref.get("document_id") or "").strip()
        if doc and doc not in seen:
            seen.add(doc)
            docs.append(doc)
    return docs
