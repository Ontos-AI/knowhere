"""One-shot harvest primitive (PLAN×NAV fusion).

Replaces the multi-step COLLECT/DISPATCH/FINISH ReAct loop with a single
policy decision per visible map node: the model returns the union of nodes to
collect and the union of nodes to dispatch to a deeper harvester in one call.
FINISH is implicit — selecting neither collect nor dispatch simply ends this
region; there is no separate round trip asking "are you done?".

Recursion is bounded by ``max_harvest_depth`` and only ever follows an
explicit DISPATCH selection (never a fixed step budget). The map already
folds large scopes to title-only and offers D* ids for internal nodes
(``nav_projection.build_map`` / ``nav_actions.build_legal_actions``), so a
node whose children overflow the display budget naturally nudges the model
toward DISPATCH — no separate "scope overflow" special case is needed here.

Depends only on the existing map/action primitives (``nav_projection``,
``nav_actions``) plus ``nav_navigate._apply_collect`` for hydration. No new
ToolSpace capability is required beyond the 5 documented in
docs/audit_plan_nav_overlap.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .nav_actions import build_legal_actions, format_actionable_map_observation
from .nav_compose import parse_collect_confidence
from .nav_plan import Subgoal
from .nav_policy import _extract_json_obj  # reuse: same tolerant JSON extraction
from .nav_projection import build_projection
from .nav_types import ActionKind, LegalAction, NavConfig, NavState, Projection

_HARVEST_PURPOSE_DEPTH0 = "nav_harvest_v1"
_HARVEST_PURPOSE_CHILD = "nav_harvest_child_v1"


def _extract_id_list_field(text: str, field: str) -> List[str]:
    """Pull a JSON string-array field even when the surrounding object is truncated."""
    m = re.search(
        rf'"{field}"\s*:\s*\[(.*?)\]',
        text or "",
        flags=re.S | re.I,
    )
    if not m:
        return []
    return [
        str(x).strip().upper()
        for x in re.findall(r'"([^"]+)"', m.group(1))
        if str(x).strip()
    ]


@dataclass
class HarvestResult:
    subgoal_id: str
    new_section_ids: List[str] = field(default_factory=list)
    visited_section_ids: List[str] = field(default_factory=list)
    n_policy_calls: int = 0
    max_depth_hit: bool = False
    # "<scope>: <reason>" per policy call this harvest tree made, joined by
    # " | " — plan_control reads this alongside collected section summaries to judge widen
    # vs drop (previously only reached AgentStep.detail, never the controller).
    reason: str = ""
    search_assets: List[Dict[str, Any]] = field(default_factory=list)
    n_assets_added: int = 0


def _actions_by_ids(actions: Sequence[LegalAction], ids: Sequence[str]) -> List[LegalAction]:
    from .nav_actions import actions_by_ids

    return actions_by_ids(list(actions), list(ids))


def _harvest_system_prompt(*, dispatch_available: bool) -> str:
    dispatch_rule = (
        "  - dispatch=D*: hand a node's subtree to a deeper harvester before "
        "deciding; use this when the node's title/summary alone does not "
        "already tell you whether the needed evidence is inside — the deeper "
        "harvester will make its own single decision over that subtree.\n"
        if dispatch_available
        else ""
    )
    return (
        "You are a retrieval harvester working on ONE retrieval subgoal inside "
        "a hierarchical document map region.\n\n"
        "In a single response, decide which visible nodes to collect as "
        "evidence for the subgoal below, and (if available) which visible "
        "nodes to hand to a deeper harvester. Selecting neither finishes this "
        "region — there is no separate finish step.\n\n"
        "=== Rules ===\n\n"
        "  - collect=C*: add the node (and its full subtree, when it is a "
        "parent) as evidence for the subgoal's contract.\n"
        f"{dispatch_rule}"
        "  - Use only action IDs shown on a node line. Never invent IDs or "
        "write raw section paths as targets.\n"
        "  - Select every node that plausibly bears on the subgoal (collect "
        "if the answer looks to be inside it already, dispatch if you are "
        "unsure and need to look deeper). Leave a node unselected only when "
        "it clearly does not relate to the subgoal.\n"
        "  - A node you leave unselected this call will not be shown again "
        "for this subgoal, so when in doubt prefer dispatch over silently "
        "skipping a node.\n"
        "  - Provide confidence in [0,1] for every collect id (object map "
        "keyed by action id, or a single scalar when there is exactly one).\n"
        "  - search_assets: optional list of {kind:\"images\"|\"tables\", "
        "query, scope?} when the subgoal needs figure/table evidence only "
        "(or in addition). kind filters Knowhere chunk_type; scope defaults "
        "to the current region and must stay inside one document "
        "(document root allowed; never the whole namespace). "
        "Asset search does not dismiss map nodes.\n\n"
        "=== End Rules ===\n\n"
        "Return ONLY one JSON object, e.g.:\n"
        '{"collect_ids":["C1"],"dispatch_ids":[],"search_assets":[],'
        '"confidence":{"C1":0.8},"reason":"short reason"}\n'
        "Do not include any explanation outside the JSON.\n\n"
        "IMPORTANT:\n"
        "1. All agent-generated text (reason) MUST be in English.\n"
        "2. Document content and section titles MUST remain in their "
        "original language.\n"
        "3. Keep reason under 25 words.\n"
    )


def _harvest_user_prompt(
    *,
    subgoal: Subgoal,
    query: str,
    observation: str,
) -> str:
    card = subgoal.contract.cardinality
    contract_line = f"{subgoal.contract.kind}" + (
        f" cardinality={card}" if card is not None else ""
    )
    return (
        f"Subgoal need: {subgoal.need or query}\n"
        f"Retrieval query: {query}\n"
        f"Contract: {contract_line}\n\n"
        f"=== Region Observation ===\n{observation}\n=== End Region Observation ===\n"
    )


def _rule_fallback_selection(
    actions: Sequence[LegalAction],
) -> Tuple[List[LegalAction], List[LegalAction]]:
    """Deterministic pick when the LLM returns no valid ids: one collect else nothing."""
    for a in actions:
        if a.kind == ActionKind.COLLECT:
            return [a], []
    return [], []


def harvest_policy_call(
    ts: Any,
    state: NavState,
    config: NavConfig,
    *,
    subgoal: Subgoal,
    query: str,
    projection: Projection,
    actions: Sequence[LegalAction],
    depth: int,
) -> Tuple[List[LegalAction], List[LegalAction], Dict[str, float], str, Dict[str, Any]]:
    """One LLM call; ``meta["search_assets"]`` holds normalized asset requests."""
    from .nav_assets import parse_search_assets
    from .nav_llm import nav_chat, resolve_nav_model
    from .nav_token_budget import NavTokenLimit, nav_token_budget_exhausted

    if nav_token_budget_exhausted():
        return [], [], {}, "token_limit", {
            "stop_reason": "token_limit",
            "search_assets": [],
            "depth": depth,
        }

    model = resolve_nav_model(
        model=(config.subagent_model if depth > 0 else config.llm_model),
        model_env=("NAV_SUBAGENT_MODEL" if depth > 0 else "NAV_LLM_MODEL"),
        fallback_envs=("NAV_LLM_MODEL",),
    )

    dispatch_available = any(a.kind == ActionKind.DISPATCH for a in actions)
    system = _harvest_system_prompt(dispatch_available=dispatch_available)
    observation = format_actionable_map_observation(
        projection, list(actions), inline_summary=projection.scope is not None
    )
    asset_ctx = str(getattr(state, "asset_observation_context", "") or "").strip()
    if asset_ctx:
        observation = f"{asset_ctx}\n\n{observation}"
    user = _harvest_user_prompt(subgoal=subgoal, query=query, observation=observation)
    purpose = _HARVEST_PURPOSE_DEPTH0 if depth == 0 else _HARVEST_PURPOSE_CHILD

    try:
        cached = nav_chat(
            purpose=purpose,
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=float(config.llm_temperature),
            max_tokens=max(
                256,
                int(getattr(config, "harvest_llm_max_tokens", 0) or 0)
                or int(config.llm_max_tokens),
            ),
            response_format={"type": "json_object"},
            context="Nav Harvest",
            usage_tag="nav_harvest",
        )
    except NavTokenLimit:
        return [], [], {}, "token_limit", {
            "stop_reason": "token_limit",
            "search_assets": [],
            "depth": depth,
        }
    text = str(cached.get("content") or "").strip()
    obj = _extract_json_obj(text) or {}

    collect_ids = [str(x).strip().upper() for x in (obj.get("collect_ids") or []) if str(x).strip()]
    dispatch_ids = [str(x).strip().upper() for x in (obj.get("dispatch_ids") or []) if str(x).strip()]
    # Truncated completions (hit llm_max_tokens mid-JSON) fail object parse and
    # used to look like an intentional empty selection — recover id arrays.
    if not collect_ids and not dispatch_ids and text:
        collect_ids = _extract_id_list_field(text, "collect_ids")
        dispatch_ids = _extract_id_list_field(text, "dispatch_ids")
    search_assets = parse_search_assets(obj.get("search_assets"))
    collect_actions = [a for a in _actions_by_ids(actions, collect_ids) if a.kind == ActionKind.COLLECT]
    dispatch_actions = [a for a in _actions_by_ids(actions, dispatch_ids) if a.kind == ActionKind.DISPATCH]
    fallback_used = False
    if (
        not collect_actions
        and not dispatch_actions
        and not collect_ids
        and not dispatch_ids
        and not search_assets
    ):
        # Genuinely empty selection == implicit finish; not a parse failure.
        pass
    elif not collect_actions and not dispatch_actions and not search_assets:
        collect_actions, dispatch_actions = _rule_fallback_selection(actions)
        fallback_used = True

    confidence = parse_collect_confidence(obj, collect_actions)
    reason = str(obj.get("reason") or "")[:300]
    meta = {
        "model": model,
        "raw": text[:500],
        "depth": depth,
        "fallback_used": fallback_used,
        "search_assets": search_assets,
    }
    return collect_actions, dispatch_actions, confidence, reason, meta


def harvest(
    ts: Any,
    state: NavState,
    config: NavConfig,
    *,
    subgoal: Subgoal,
    entry_scope: Optional[str],
    query: str,
    steps_out: Optional[List[Any]] = None,
    allowed_section_ids: Optional[Set[str]] = None,
) -> HarvestResult:
    """Single-decision-per-node evidence harvest for one subgoal."""
    result = HarvestResult(subgoal_id=subgoal.id)
    from .nav_address import NavLevel, address_level

    if entry_scope is None:
        initial_depth = 0
    else:
        level = address_level(ts, entry_scope)
        if level in (NavLevel.NAMESPACE, NavLevel.DOCUMENT):
            initial_depth = 0
        else:
            # SECTION / unknown → already inside a document.
            initial_depth = 1
    _harvest_node(
        ts,
        state,
        config,
        subgoal=subgoal,
        node_scope=entry_scope,
        query=query,
        depth=initial_depth,
        steps_out=steps_out,
        result=result,
        allowed_section_ids=allowed_section_ids,
    )
    return result


def _harvest_node(
    ts: Any,
    state: NavState,
    config: NavConfig,
    *,
    subgoal: Subgoal,
    node_scope: Optional[str],
    query: str,
    depth: int,
    steps_out: Optional[List[Any]],
    result: HarvestResult,
    allowed_section_ids: Optional[Set[str]] = None,
) -> None:
    from .nav_navigate import _apply_collect  # late import avoids cycle
    from .nav_token_budget import stamp_step_detail

    max_depth = max(0, int(getattr(config, "max_harvest_depth", 0) or 0))
    show_harvested = bool(config.is_checklist)
    subgoal_dismissed = state.subgoal_dismissed_section_ids.get(subgoal.id, set())
    projection = build_projection(
        ts,
        doc_id=state.doc_id,
        query=query,
        scope=node_scope,
        config=config,
        map_scores=state.map_scores,
        collected_section_ids=state.collected_section_ids,
        dismissed_section_ids=state.dismissed_section_ids | subgoal_dismissed,
        highlight_ids=state.highlight_ids,
        harvested_section_ids=state.harvested_owner_subgoal if show_harvested else None,
        allowed_section_ids=allowed_section_ids,
    )
    actions = build_legal_actions(
        state, projection, step_idx=0, config=config, depth=depth, ts=ts
    )
    actionable = [a for a in actions if a.kind != ActionKind.FINISH]
    if not actionable:
        result.reason = result.reason or "no_legal_actions"
        return

    collect_actions, dispatch_actions, confidence, reason, meta = harvest_policy_call(
        ts,
        state,
        config,
        subgoal=subgoal,
        query=query,
        projection=projection,
        actions=actions,
        depth=depth,
    )
    result.n_policy_calls += 1
    if reason:
        tag = node_scope or "root"
        result.reason = f"{result.reason} | {tag}: {reason}" if result.reason else f"{tag}: {reason}"

    search_assets = list(meta.get("search_assets") or [])
    result.search_assets.extend(search_assets)

    # Only the current node and its direct children are an actual decision at
    # this call — deeper rows are shown for context inside the same
    # full-depth map but belong to whichever recursive call lands on their
    # own direct parent. Dismissing them here would pre-empt that call (e.g.
    # a leaf two levels down would never get its own chance once its
    # grandparent is DISPATCHed instead of collected).
    # search_assets does not count as selecting a map node (F5).
    depth_from_scope = {v.section_id: v.depth_from_scope for v in projection.tree_sections}
    decided_now = [a for a in actionable if depth_from_scope.get(a.section_id or "", 99) <= 1]
    selected_ids = {a.section_id for a in (*collect_actions, *dispatch_actions) if a.section_id}
    unselected_ids = {
        a.section_id for a in decided_now if a.section_id and a.section_id not in selected_ids
    }
    if unselected_ids:
        state.subgoal_dismissed_section_ids.setdefault(subgoal.id, set()).update(unselected_ids)

    if steps_out is not None:
        from ._compat import AgentStep  # type: ignore

        observation = format_actionable_map_observation(
            projection, list(actions), inline_summary=projection.scope is not None
        )
        steps_out.append(
            AgentStep(
                step_idx=len(steps_out) + 1,
                action="harvest",
                detail=stamp_step_detail({
                    "subgoal_id": subgoal.id,
                    "scope": node_scope,
                    "depth": depth,
                    "collect_ids": [a.action_id for a in collect_actions],
                    "dispatch_ids": [a.action_id for a in dispatch_actions],
                    "search_assets": search_assets,
                    "confidence": confidence,
                    "reason": reason,
                    "projection_chars": len(observation),
                    "legal_actions_preview": [a.prompt_line() for a in actions[:16]],
                    "visible_section_ids": [
                        str(v.section_id)
                        for v in projection.tree_sections
                        if getattr(v, "section_id", None)
                    ],
                    **{k: v for k, v in meta.items() if k != "search_assets"},
                }),
            )
        )

    if collect_actions:
        conf_by_sid = {
            str(a.section_id): float(confidence.get(a.action_id.upper(), 0.0))
            for a in collect_actions
            if a.section_id
        }
        primary = collect_actions[0]
        primary.metadata = dict(primary.metadata or {})
        primary.metadata["batch_actions"] = collect_actions
        primary.metadata["confidence_by_section"] = conf_by_sid
        cdetail = _apply_collect(ts, state, primary, config)
        new_roots = list(cdetail.get("collect_section_ids") or [])
        result.new_section_ids.extend(new_roots)
        if show_harvested:
            for sid in new_roots:
                state.harvested_owner_subgoal[sid] = subgoal.id

    if search_assets:
        from .nav_assets import apply_search_assets

        n_added, asset_trace = apply_search_assets(
            ts,
            state,
            config,
            requests=search_assets,
            default_scope=node_scope,
        )
        result.n_assets_added += n_added
        if steps_out is not None and asset_trace:
            from ._compat import AgentStep  # type: ignore

            steps_out.append(
                AgentStep(
                    step_idx=len(steps_out) + 1,
                    action="search_assets",
                    detail=stamp_step_detail({
                        "subgoal_id": subgoal.id,
                        "scope": node_scope,
                        "n_added": n_added,
                        "requests": asset_trace,
                    }),
                )
            )

    if dispatch_actions and depth >= max_depth:
        result.max_depth_hit = True
        return

    from .nav_address import next_dispatch_depth

    for act in dispatch_actions:
        sid = str(act.section_id or "").strip()
        if not sid or sid == str(node_scope or ""):
            continue
        result.visited_section_ids.append(sid)
        before_collected = len(result.new_section_ids)
        child_depth = next_dispatch_depth(
            ts,
            parent_doc_id=str(state.doc_id or ""),
            parent_scope=str(node_scope or ""),
            child_id=sid,
            depth=depth,
        )
        _harvest_node(
            ts,
            state,
            config,
            subgoal=subgoal,
            node_scope=sid,
            query=query,
            depth=child_depth,
            steps_out=steps_out,
            result=result,
            allowed_section_ids=allowed_section_ids,
        )
        # Whole dispatched subtree came up empty: a dead end, not just "not
        # yet explored" — dismiss it too so a later widen doesn't re-dispatch
        # into the same branch.
        if len(result.new_section_ids) == before_collected:
            state.subgoal_dismissed_section_ids.setdefault(subgoal.id, set()).add(sid)
