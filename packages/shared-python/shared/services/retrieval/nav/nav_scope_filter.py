"""Bounded WHERE self-correction loop (pre-harvest).

The policy writes or revises a ``NodeFilter``. Apply is deterministic. The
loop stops at ``filter_max_rounds``, token exhaustion, explicit fallback, or
a done-in-band settle. Decision is cardinality-driven; the agent may override.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence

from .nav_node_filter import (
    FilterResult,
    NodeFilter,
    apply_node_filter,
    field_predicate,
    node_filter,
    render_submap_observation,
)
from .nav_types import NavConfig

ScopeDecision = Literal["collect_all", "scoped_harvest", "fallback"]
_SCOPE_FILTER_PURPOSE = "nav_scope_filter_v1"
_DECISIONS = {"collect_all", "scoped_harvest", "fallback"}


@dataclass
class ScopeFilterOutcome:
    decision: ScopeDecision
    settled_section_ids: List[str] = field(default_factory=list)
    settled_doc_ids: List[str] = field(default_factory=list)
    rounds: int = 0
    last_result: Optional[FilterResult] = None
    reason: str = ""


def parse_node_filter(obj: Dict[str, Any] | None) -> Optional[NodeFilter]:
    if not isinstance(obj, dict):
        return None
    raw = obj.get("predicates")
    if raw is None:
        raw = obj.get("filter")
    if not isinstance(raw, list):
        return None
    preds = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        terms = item.get("terms") or []
        if isinstance(terms, str):
            terms = [terms]
        if not isinstance(terms, list):
            continue
        try:
            preds.append(
                field_predicate(
                    str(item.get("field") or ""),
                    [str(t) for t in terms],
                    str(item.get("match") or "substring"),
                )
            )
        except ValueError:
            continue
    if not preds:
        return None
    return node_filter(preds)


def run_scope_filter(
    ts: Any,
    config: NavConfig,
    *,
    query: str,
    doc_ids: Sequence[str],
    map_observation: str = "",
    seed_filter: Optional[NodeFilter] = None,
    steps_out: Optional[List[Any]] = None,
) -> ScopeFilterOutcome:
    """Apply → observe → revise until settle, fallback, or the round cap."""
    if not bool(getattr(config, "enable_node_filter", False)):
        return ScopeFilterOutcome(decision="fallback", reason="disabled")

    from .nav_token_budget import nav_token_budget_exhausted, stamp_step_detail

    max_rounds = max(1, int(getattr(config, "filter_max_rounds", 3) or 3))
    min_hits = max(0, int(getattr(config, "filter_min_hits", 1) or 0))
    max_hits = max(min_hits, int(getattr(config, "filter_max_hits", 40) or 0))
    wanted = [str(did).strip() for did in doc_ids if str(did).strip()]
    map_text = str(map_observation or "").strip() or _compact_map(ts, wanted)
    current = seed_filter
    last_result: Optional[FilterResult] = None
    last_obs = ""
    last_decision: Optional[ScopeDecision] = None
    last_reason = ""

    if current is None:
        action = _scope_filter_policy_call(
            config,
            query=query,
            full_map=map_text,
            last_filter=None,
            last_result=None,
            last_observation="",
            round_idx=0,
            max_rounds=max_rounds,
            include_full_map=True,
        )
        current = action.get("filter")
        last_decision = action.get("decision")
        last_reason = str(action.get("reason") or "")
        if action.get("kind") == "fallback" or current is None:
            return ScopeFilterOutcome(
                decision="fallback",
                reason=last_reason or "policy_fallback",
            )

    for round_idx in range(1, max_rounds + 1):
        if nav_token_budget_exhausted():
            return ScopeFilterOutcome(
                decision="fallback",
                settled_section_ids=list(last_result.matched_section_ids)
                if last_result
                else [],
                settled_doc_ids=list(last_result.matched_doc_ids) if last_result else [],
                rounds=round_idx - 1,
                last_result=last_result,
                reason="token_limit",
            )
        assert current is not None
        result = apply_node_filter(ts, wanted, current)
        last_result = result
        last_obs = render_submap_observation(ts, result, doc_ids=wanted)
        in_band = min_hits <= result.cardinality <= max_hits
        if steps_out is not None:
            from ._compat import AgentStep

            steps_out.append(
                AgentStep(
                    step_idx=len(steps_out) + 1,
                    action="node_filter",
                    detail=stamp_step_detail(
                        {
                            "round": round_idx,
                            "predicates": _filter_payload(current),
                            "fields": sorted(
                                {p.field for p in current.predicates}
                            ),
                            "cardinality": result.cardinality,
                            "truncated": result.truncated,
                            "failed_predicates": list(result.failed_predicates),
                            "matched_section_ids": list(result.matched_section_ids),
                            "action": "",
                            "decision": "",
                            "reason": "",
                        }
                    ),
                )
            )

        is_last = round_idx >= max_rounds
        if is_last:
            if steps_out:
                steps_out[-1].detail["action"] = "max_rounds"
            return _settle(
                result,
                in_band=in_band,
                agent_decision=last_decision,
                min_hits=min_hits,
                rounds=round_idx,
                reason=("max_rounds" if in_band else "max_rounds_out_of_band"),
                steps_out=steps_out,
            )

        action = _scope_filter_policy_call(
            config,
            query=query,
            full_map=map_text,
            last_filter=current,
            last_result=result,
            last_observation=last_obs,
            round_idx=round_idx,
            max_rounds=max_rounds,
            include_full_map=False,
        )
        if action.get("kind") == "widen":
            # Agent judged the sub-map too narrow: one re-look with the full
            # map, same round. A second widen settles on whatever it returns.
            action = _scope_filter_policy_call(
                config,
                query=query,
                full_map=map_text,
                last_filter=current,
                last_result=result,
                last_observation=last_obs,
                round_idx=round_idx,
                max_rounds=max_rounds,
                include_full_map=True,
            )
            if action.get("kind") == "widen":
                if action.get("filter") is not None:
                    action["kind"] = "filter"
                else:
                    action["kind"] = "fallback"
                    action["reason"] = action.get("reason") or "widen_without_filter"
        last_decision = action.get("decision")
        last_reason = str(action.get("reason") or "")
        kind = str(action.get("kind") or "")
        if steps_out:
            steps_out[-1].detail["action"] = kind
            steps_out[-1].detail["reason"] = last_reason
        if kind == "fallback":
            if steps_out:
                steps_out[-1].detail["decision"] = "fallback"
            return ScopeFilterOutcome(
                decision="fallback",
                settled_section_ids=list(result.matched_section_ids),
                settled_doc_ids=list(result.matched_doc_ids),
                rounds=round_idx,
                last_result=result,
                reason=last_reason or "policy_fallback",
            )
        if kind == "done":
            return _settle(
                result,
                in_band=in_band,
                agent_decision=last_decision,
                min_hits=min_hits,
                rounds=round_idx,
                reason=last_reason or "done",
                steps_out=steps_out,
            )
        nxt = action.get("filter")
        if nxt is not None:
            current = nxt

    assert last_result is not None
    in_band = min_hits <= last_result.cardinality <= max_hits
    return _settle(
        last_result,
        in_band=in_band,
        agent_decision=last_decision,
        min_hits=min_hits,
        rounds=max_rounds,
        reason=last_reason or "max_rounds",
        steps_out=steps_out,
    )


def _settle(
    result: FilterResult,
    *,
    in_band: bool,
    agent_decision: Optional[ScopeDecision],
    min_hits: int,
    rounds: int,
    reason: str,
    steps_out: Optional[List[Any]],
) -> ScopeFilterOutcome:
    if not in_band or result.cardinality <= 0:
        decision: ScopeDecision = "fallback"
        settle_reason = reason or "out_of_band"
    elif agent_decision in _DECISIONS:
        decision = agent_decision
        settle_reason = reason or "agent"
        if decision == "fallback":
            settle_reason = reason or "agent_fallback"
    elif result.cardinality <= min_hits:
        decision = "collect_all"
        settle_reason = reason or "small_cardinality"
    else:
        decision = "scoped_harvest"
        settle_reason = reason or "medium_cardinality"
    if steps_out:
        steps_out[-1].detail["decision"] = decision
        steps_out[-1].detail["reason"] = settle_reason
    return ScopeFilterOutcome(
        decision=decision,
        settled_section_ids=list(result.matched_section_ids),
        settled_doc_ids=list(result.matched_doc_ids),
        rounds=rounds,
        last_result=result,
        reason=settle_reason,
    )


def _scope_filter_policy_call(
    config: NavConfig,
    *,
    query: str,
    full_map: str,
    last_filter: Optional[NodeFilter],
    last_result: Optional[FilterResult],
    last_observation: str,
    round_idx: int,
    max_rounds: int,
    include_full_map: bool,
) -> Dict[str, Any]:
    from .nav_llm import nav_chat, resolve_nav_model
    from .nav_policy import _extract_json_obj
    from .nav_token_budget import NavTokenLimit, nav_token_budget_exhausted

    if nav_token_budget_exhausted():
        return {"kind": "fallback", "reason": "token_limit"}

    model = resolve_nav_model(
        model=config.llm_model,
        model_env="NAV_LLM_MODEL",
        fallback_envs=("NAV_LLM_MODEL",),
    )
    user = f"Query: {query}\nRound: {round_idx}/{max_rounds}\n"
    if last_filter is not None:
        user += f"Last filter: {json.dumps(_filter_payload(last_filter), ensure_ascii=False)}\n"
    if last_result is not None:
        user += f"Last hits: {last_result.cardinality}\n"
    if last_observation:
        user += f"=== Sub-map (hits of last filter) ===\n{last_observation}\n"
    if include_full_map:
        user += f"=== Full map ===\n{full_map}\n=== End Full Map ===\n"
    try:
        cached = nav_chat(
            purpose=_SCOPE_FILTER_PURPOSE,
            model=model,
            messages=[
                {"role": "system", "content": _scope_filter_system_prompt(seed=last_filter is None)},
                {"role": "user", "content": user},
            ],
            temperature=float(config.llm_temperature),
            max_tokens=max(256, int(config.llm_max_tokens or 256)),
            response_format={"type": "json_object"},
            context="Nav Scope Filter",
            usage_tag="nav_scope_filter",
        )
    except NavTokenLimit:
        return {"kind": "fallback", "reason": "token_limit"}

    text = str(cached.get("content") or "").strip()
    obj = _extract_json_obj(text) or {}
    kind = str(obj.get("action") or obj.get("kind") or "filter").strip().lower()
    if kind not in {"filter", "done", "widen", "fallback"}:
        kind = "filter"
    decision_raw = str(obj.get("decision") or "").strip().lower()
    decision: Optional[ScopeDecision] = (
        decision_raw if decision_raw in _DECISIONS else None
    )
    parsed = parse_node_filter(obj)
    return {
        "kind": kind,
        "filter": parsed,
        "decision": decision,
        "reason": str(obj.get("reason") or "")[:300],
        "raw": text,
    }


def _scope_filter_system_prompt(*, seed: bool) -> str:
    field_schema = (
        '"path"' if seed else '"path|summary"'
    )
    field_rule = (
        "Match on section paths only (filenames and title chains)."
        if seed
        else "Fields AND together; terms inside one field OR together."
    )
    base = (
        "You write a WHERE filter over document sections. Return json.\n"
        "Schema: {\"action\":\"filter|done|widen|fallback\",\"predicates\":"
        f"[{{\"field\":{field_schema},\"terms\":[\"...\"],\"match\":\"substring|regex\"}}],"
        "\"decision\":\"collect_all|scoped_harvest|fallback\",\"reason\":\"...\"}\n"
        f"{field_rule}\n"
        "Write terms as natural-language words from the query or the map "
        "(entities, topics, aliases); do not rely on section numbering alone.\n"
    )
    if seed:
        return base + (
            "Write the first filter for the query against the full map. "
            "action=filter returns it; action=fallback only when no path "
            "predicate can isolate the target sections."
        )
    return base + (
        "You see the sub-map hit by the last filter; judge by its content:\n"
        "- sub-map covers the query -> action=done\n"
        "- sub-map has off-topic nodes -> action=filter with a narrower "
        "revision of the last filter\n"
        "- sub-map looks too narrow or misses parts of the query -> "
        "action=widen to see the full map once, then revise\n"
        "Revise the last filter, never restart from the query. "
        "action=fallback only when no predicate can isolate the target sections."
    )


def _filter_payload(nf: NodeFilter) -> List[Dict[str, Any]]:
    return [
        {"field": p.field, "terms": list(p.terms), "match": p.match}
        for p in nf.predicates
    ]


def _compact_map(ts: Any, doc_ids: Sequence[str]) -> str:
    path_fn = getattr(ts, "path_titles", None)
    structure_fn = getattr(ts, "get_structure", None)
    lines: List[str] = []

    def path_of(sid: str, doc_id: str) -> str:
        if not callable(path_fn):
            return sid
        try:
            return str(path_fn(sid, doc_id) or sid)
        except TypeError:
            return str(path_fn(sid) or sid)

    def summary_of(sid: str) -> str:
        if not callable(structure_fn):
            return ""
        try:
            st = structure_fn(sid) or {}
            return str(st.get("summary") or "").strip()
        except Exception:
            return ""

    provider = getattr(ts, "_provider", None)
    child_fn = getattr(provider, "children", None) if provider is not None else None
    root_fn = getattr(ts, "sections_for_doc", None)

    for doc_id in doc_ids:
        lines.append(path_of(doc_id, doc_id))
        stack = [str(s) for s in (root_fn(doc_id) if callable(root_fn) else []) if str(s)]
        seen: set[str] = set()
        while stack:
            sid = stack.pop(0)
            if not sid or sid in seen:
                continue
            seen.add(sid)
            path = path_of(sid, doc_id)
            summary = summary_of(sid)
            line = path if not summary else f"{path} | {summary}"
            lines.append(line)
            kids = [str(c) for c in (child_fn(sid) if callable(child_fn) else []) if str(c)]
            stack[0:0] = kids
    return "\n".join(lines)
