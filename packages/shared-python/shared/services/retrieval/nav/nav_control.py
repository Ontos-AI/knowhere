"""Merged check authority (PLAN×NAV fusion): one planner decision per wave.

``plan_control`` is the single authority that reconciles each wave's new
evidence against the plan's coverage checklist. It returns one decision per
subgoal plus one global decision. ``REPLAN`` can only originate here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence

from .nav_plan import RetrievalPlan, Subgoal
from .nav_policy import _extract_json_obj
from .nav_types import NavConfig, NavState

SubgoalDecisionKind = Literal["accept", "widen", "drop"]
GlobalDecisionKind = Literal["continue", "replan", "done"]

_CONTROL_PURPOSE = "nav_plan_control_v2"
_SUBGOAL_DECISIONS = {"accept", "widen", "drop"}
_GLOBAL_DECISIONS = {"continue", "replan", "done"}


@dataclass
class SubgoalDecision:
    subgoal_id: str
    decision: SubgoalDecisionKind = "accept"
    note: str = ""


@dataclass
class PlanControlDecision:
    per_subgoal: Dict[str, SubgoalDecision] = field(default_factory=dict)
    global_action: GlobalDecisionKind = "continue"
    reason: str = ""
    raw: str = ""


def _section_summary_text(ts: Any, section_id: str) -> str:
    """Full stored section summary (already head/tail clipped at build time)."""
    sid = str(section_id or "").strip()
    if not sid:
        return ""
    try:
        from .nav_address import owner_document
        from section_summary_store import get_summary

        doc = owner_document(ts, sid, "") if ts is not None else ""
        text = str(get_summary(sid, doc_id=doc) or "").strip()
        if text:
            return text
    except Exception:
        pass
    if ts is not None:
        try:
            st = ts.get_structure(sid) or {}
            text = str(st.get("summary") or "").strip()
            if text:
                return text
            # Last resort: title/preview only (still no raw-body truncation here).
            return str(st.get("preview") or "").strip()
        except Exception:
            return ""
    return ""


def _digest_collected_summaries(
    ts: Any,
    *,
    collected_section_ids: Optional[Sequence[str]] = None,
) -> str:
    """One line per explicit COLLECT target: path + its prebuilt summary.

    Hydration descendants are intentionally omitted — control only needs the
    nodes the harvester chose, not every leaf sucked in under them.
    """
    sids = [str(s).strip() for s in (collected_section_ids or []) if str(s).strip()]
    if not sids:
        # No explicit COLLECTs this wave: do not invent digest lines from
        # hydrated chunk owners (that reintroduces the descendant flood).
        return ""
    lines: List[str] = []
    for sid in sids:
        title = _section_display_title(ts, sid)
        summary = _section_summary_text(ts, sid)
        if summary:
            lines.append(f"- {title}: {summary}")
        else:
            lines.append(f"- {title}: (no summary)")
    return "\n".join(lines)


def _section_display_title(ts: Any, section_id: str) -> str:
    """Human title for control digest — never parse section_id strings."""
    sid = str(section_id or "").strip()
    if not sid:
        return ""
    if ts is not None:
        try:
            st = ts.get_structure(sid) or {}
            title = str(st.get("preview") or st.get("title") or "").strip()
            if title:
                return title
        except Exception:
            pass
    return sid


def _wave_subgoal_block(
    subgoal: Subgoal,
    *,
    digest: str,
    harvest_meta: Optional[Dict[str, Any]],
    attempt_count: int,
) -> str:
    card = subgoal.contract.cardinality
    contract_line = f"{subgoal.contract.kind}" + (
        f" cardinality={card}" if card is not None else ""
    )
    lines = [
        f"[{subgoal.id}] need: {subgoal.need}",
        f"  contract: {contract_line}",
        "  search_space: shared (no per-subgoal scope/anchor)",
        f"  attempt: {attempt_count}",
    ]
    if harvest_meta:
        lines.append(
            "  harvest: "
            f"visited={len(harvest_meta.get('visited_section_ids') or [])} "
            f"policy_calls={harvest_meta.get('n_policy_calls', 0)}"
        )
        harvest_reason = str(harvest_meta.get("reason") or "").strip()
        if harvest_reason:
            lines.append(f"  harvest_reason: {harvest_reason}")
    lines.append(f"  explicit_collect_summaries:\n{digest.strip() or '  (empty)'}")
    return "\n".join(lines)


def _plan_overview_block(plan: RetrievalPlan, state: NavState) -> str:
    lines = ["coverage_checklist:"]
    checklist = list(getattr(plan, "coverage_checklist", None) or [])
    if checklist:
        for item in checklist:
            lines.append(f"- {item.id}: {item.fact}")
    else:
        lines.append("- (none)")
    lines.append("subgoals:")
    for sg in plan.subgoals:
        status = (
            "satisfied"
            if sg.id in state.satisfied_subgoal_ids
            else "attempted" if sg.id in state.attempted_subgoal_ids else "pending"
        )
        lines.append(f"- {sg.id}: {sg.need} [{status}]")
    return "\n".join(lines)


def _control_system_prompt() -> str:
    return (
        "You are the single retrieval-plan controller for one wave of a "
        "hierarchical document retrieval episode. You replace all separate "
        "per-region stop/retry judgments with one decision.\n\n"
        "The plan has a global coverage_checklist (facts that must appear in "
        "episode evidence) and one shared search space. For each subgoal in "
        "this wave you are shown: its need/contract, attempt count, "
        "the harvester's own explanation (harvest_reason), and the section "
        "summaries for nodes explicitly COLLECTed THIS wave only (never "
        "hydration descendants, and never older evidence from other subgoals; "
        "summaries are the prebuilt node summaries).\n\n"
        "=== Per-subgoal decisions ===\n"
        "  - accept: this subgoal's need is covered by this wave's evidence.\n"
        "  - widen: not yet covered; keep the subgoal unsettled for another "
        "harvest from the shared root. Prior dead-ends stay dismissed; PLAN "
        "rewrites this subgoal's retrieval_query from your note. "
        "Do not name an entry point.\n"
        "  - drop: not covered and further attempts are unlikely to help "
        "(e.g. evidence is structurally absent, or harvest_reason shows the "
        "search has already reached the document root with nothing found); "
        "stop trying this subgoal.\n\n"
        "=== Global decision ===\n"
        "  - continue: proceed to the next wave with current subgoal set.\n"
        "  - replan: the retrieval plan's decomposition itself is wrong "
        "(e.g. missing checklist facts, wrong dependencies) and needs regenerating. "
        "Only choose this for a structural plan problem, not for an "
        "individual subgoal's evidence gap (use widen/drop for those).\n"
        "  - done: the coverage_checklist is met (or cannot be met "
        "further); stop the episode.\n\n"
        "Return ONLY one JSON object:\n"
        "{\n"
        '  "subgoals": {"s1": {"decision": "accept|widen|drop", '
        '"note": "..."}},\n'
        '  "global": "continue|replan|done",\n'
        '  "reason": "..."\n'
        "}\n"
        "Do not include any explanation outside the JSON.\n\n"
        "IMPORTANT:\n"
        "1. All agent-generated text (note/reason) MUST be in English.\n"
        "2. Keep reason under 30 words; keep each note under 15 words.\n"
    )


def _parse_control_decision(obj: Dict[str, Any], subgoal_ids: Sequence[str]) -> PlanControlDecision:
    per_subgoal: Dict[str, SubgoalDecision] = {}
    raw_sub = obj.get("subgoals") if isinstance(obj, dict) else None
    if isinstance(raw_sub, dict):
        for sid in subgoal_ids:
            row = raw_sub.get(sid)
            if not isinstance(row, dict):
                continue
            decision = str(row.get("decision") or "").strip().lower()
            if decision not in _SUBGOAL_DECISIONS:
                decision = "accept"
            per_subgoal[sid] = SubgoalDecision(
                subgoal_id=sid,
                decision=decision,  # type: ignore[arg-type]
                note=str(row.get("note") or "")[:200],
            )
    global_action = str(obj.get("global") or "").strip().lower()
    if global_action not in _GLOBAL_DECISIONS:
        global_action = "continue"
    return PlanControlDecision(
        per_subgoal=per_subgoal,
        global_action=global_action,  # type: ignore[arg-type]
        reason=str(obj.get("reason") or "")[:300],
    )


def _fallback_decision(
    subgoal_ids: Sequence[str],
    signals: Dict[str, Any],
) -> PlanControlDecision:
    """Deterministic fallback when the LLM call fails or returns malformed JSON.

    Only mechanical signal left: this wave collected evidence → accept,
    otherwise widen. Checklist judgment is not attempted offline.
    """
    per_subgoal: Dict[str, SubgoalDecision] = {}
    for sid in subgoal_ids:
        sig = signals.get(sid)
        has_evidence = (
            int(getattr(sig, "chars_used", 0) or 0) > 0 if sig is not None else False
        )
        per_subgoal[sid] = SubgoalDecision(
            subgoal_id=sid,
            decision="accept" if has_evidence else "widen",
        )
    return PlanControlDecision(per_subgoal=per_subgoal, global_action="continue", reason="fallback")


def plan_control(
    ts: Any,
    state: NavState,
    config: NavConfig,
    *,
    plan: RetrievalPlan,
    wave_outputs: Sequence[Dict[str, Any]],
) -> PlanControlDecision:
    """One LLM call per wave: per-subgoal accept/widen/drop + global signal."""
    from .nav_token_budget import NavTokenLimit, nav_token_budget_exhausted

    by_id = {s.id: s for s in plan.subgoals}
    subgoal_ids = [str(item.get("subgoal_id")) for item in wave_outputs]
    signals = {str(item.get("subgoal_id")): item.get("result") for item in wave_outputs}
    if not subgoal_ids:
        return PlanControlDecision(global_action="continue")
    if nav_token_budget_exhausted():
        decision = _fallback_decision(subgoal_ids, signals)
        decision.global_action = "done"
        decision.reason = "token_limit"
        return decision

    blocks: List[str] = []
    for item in wave_outputs:
        sid = str(item.get("subgoal_id"))
        sg = by_id.get(sid)
        if sg is None:
            continue
        result = item.get("result")
        # Control digest = explicit COLLECT targets only (not hydrated descendants).
        if hasattr(result, "explicit_collect_ids"):
            digest_ids = list(getattr(result, "explicit_collect_ids", None) or [])
        else:
            digest_ids = list(getattr(result, "collected_section_ids", None) or [])
        digest = _digest_collected_summaries(
            ts,
            collected_section_ids=digest_ids,
        )
        blocks.append(
            _wave_subgoal_block(
                sg,
                digest=digest,
                harvest_meta=item.get("harvest"),
                attempt_count=int(state.subgoal_attempt_counts.get(sid, 0)),
            )
        )
    user = (
        f"=== Plan Overview ===\n{_plan_overview_block(plan, state)}\n"
        "=== End Plan Overview ===\n\n"
        f"=== This Wave ({len(wave_outputs)} subgoal(s)) ===\n"
        + "\n\n".join(blocks)
        + "\n=== End This Wave ===\n\n"
        "Return the control decision JSON."
    )

    try:
        from .nav_llm import (  # type: ignore
            nav_chat,
            planner_output_max_tokens,
            resolve_nav_model,
            resolve_nav_thinking_mode,
        )

        model = resolve_nav_model(
            model=config.planner_model,
            model_env="NAV_PLANNER_MODEL",
            fallback_envs=("NAV_LLM_MODEL",),
        )
        # Control is short JSON (accept/widen/drop); thinking only on plan_query/replan.
        max_tokens = planner_output_max_tokens(
            int(getattr(config, "planner_llm_max_tokens", 0) or 0)
            or int(config.llm_max_tokens or 256)
        )
        timeout_s = float(os.environ.get("NAV_PLANNER_TIMEOUT_SECONDS", "").strip() or "0")
        if timeout_s <= 0:
            timeout_s = (
                300.0
                if resolve_nav_thinking_mode(role="planner") == "enabled"
                else 90.0
            )
        cached = nav_chat(
            purpose=_CONTROL_PURPOSE,
            model=model,
            messages=[
                {"role": "system", "content": _control_system_prompt()},
                {"role": "user", "content": user},
            ],
            temperature=float(config.llm_temperature),
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            thinking_role="planner",
            context="Nav Plan Control",
            api_key_env="NAV_PLANNER_API_KEY",
            base_url_env="NAV_PLANNER_BASE_URL",
            timeout=timeout_s,
            usage_tag="nav_plan_control",
        )
        text = str(cached.get("content") or "").strip()
        if not text:
            reasoning = str(cached.get("reasoning_content") or "").strip()
            if reasoning and _extract_json_obj(reasoning):
                text = reasoning
        obj = _extract_json_obj(text) or {}
        decision = _parse_control_decision(obj, subgoal_ids)
        decision.raw = text[:1000]
        # Any subgoal the model omitted still needs an explicit decision.
        for sid in subgoal_ids:
            if sid not in decision.per_subgoal:
                sig = signals.get(sid)
                has_evidence = (
                    int(getattr(sig, "chars_used", 0) or 0) > 0
                    if sig is not None
                    else False
                )
                decision.per_subgoal[sid] = SubgoalDecision(
                    subgoal_id=sid, decision="accept" if has_evidence else "widen"
                )
        return decision
    except NavTokenLimit:
        decision = _fallback_decision(subgoal_ids, signals)
        decision.global_action = "done"
        decision.reason = "token_limit"
        return decision
    except Exception:
        return _fallback_decision(subgoal_ids, signals)
