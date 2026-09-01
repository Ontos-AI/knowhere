"""M4/M5: wave orchestration over a RetrievalPlan.

Execution order = dependency DAG ∩ soft prefer_after. Each subgoal runs its own
harvest so evidence attribution stays per-subgoal. Slot values are
extracted only when a later subgoal references them; checklist acceptance is
owned by ``plan_control``.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from .nav_token_budget import stamp_step_detail
from .nav_plan import (
    RetrievalPlan,
    Subgoal,
    bind_slots,
    plan_query,
    refine_subgoal_query,
    unbound_slots,
)
from .nav_types import NavConfig, NavState, SubgoalResult
from .nav_verify import apply_bindings_from_result, build_subgoal_result

_SLOT_STRIP_RE = re.compile(r"\{\{\s*[^}]+\s*\}\}")
_logger = logging.getLogger(__name__)


def ready_subgoal_ids(
    plan: RetrievalPlan,
    *,
    satisfied: Set[str],
    attempted: Optional[Set[str]] = None,
    dropped: Optional[Set[str]] = None,
) -> List[str]:
    """Subgoals eligible to run now (deps settled, not yet finished).

    F1: a dependency only needs to be *settled* (``satisfied`` or ``dropped``).
    Widen leaves a subgoal out of ``attempted`` so it stays ready next wave.
    """
    settled = set(satisfied) | set(dropped or ())
    known = {s.id for s in plan.subgoals}
    done = set(attempted or ()) | set(satisfied) | set(dropped or ())
    out: List[str] = []
    for sg in plan.subgoals:
        if sg.id in done:
            continue
        deps = [d for d in (sg.depends_on or []) if d in known]
        if any(d not in settled for d in deps):
            continue
        out.append(sg.id)
    return order_ready_by_prefer_after(plan, out)


def order_ready_by_prefer_after(
    plan: RetrievalPlan,
    ready_ids: Sequence[str],
) -> List[str]:
    """Soft ordering: if A prefer_after B and both ready, B before A."""
    ready = [str(x) for x in ready_ids]
    if len(ready) <= 1:
        return ready
    by = {s.id: s for s in plan.subgoals}
    ready_set = set(ready)
    # Kahn over soft edges B -> A when A prefer_after B.
    indeg = {i: 0 for i in ready}
    edges: Dict[str, List[str]] = {i: [] for i in ready}
    for sid in ready:
        sg = by.get(sid)
        if sg is None:
            continue
        for pred in sg.prefer_after or []:
            if pred in ready_set and pred != sid:
                edges[pred].append(sid)
                indeg[sid] += 1
    queue = [i for i in ready if indeg[i] == 0]
    ordered: List[str] = []
    seen = set()
    while queue:
        # Stable: keep original relative order among zero-indegree nodes.
        queue.sort(key=lambda x: ready.index(x))
        node = queue.pop(0)
        if node in seen:
            continue
        seen.add(node)
        ordered.append(node)
        for nxt in edges.get(node, []):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    for sid in ready:
        if sid not in seen:
            ordered.append(sid)
    return ordered


def _set_focus(state: NavState, subgoal: Subgoal, retrieval_query: str) -> None:
    state.focus_subgoal_id = subgoal.id
    state.focus_subgoal_need = subgoal.need or retrieval_query
    state.focus_retrieval_query = retrieval_query
    kind = subgoal.contract.kind
    card = subgoal.contract.cardinality
    state.focus_contract_kind = str(kind or "")
    state.focus_subgoal_contract = (
        f"{kind}" + (f" cardinality={card}" if card is not None else "")
    )


def _clear_focus(state: NavState) -> None:
    state.focus_subgoal_id = ""
    state.focus_subgoal_need = ""
    state.focus_subgoal_contract = ""
    state.focus_retrieval_query = ""
    state.focus_contract_kind = ""


def _unbound_retrieval_query(subgoal: Subgoal) -> str:
    """Drop unresolved slot braces for REBIND degrade."""
    raw = _SLOT_STRIP_RE.sub(" ", subgoal.retrieval_query or "").strip()
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw or (subgoal.need or "").strip() or subgoal.retrieval_query


def _resolve_subgoal_query(state: NavState, subgoal: Subgoal) -> str:
    query = bind_slots(subgoal.retrieval_query, state.slot_bindings)
    if unbound_slots(query):
        query = _unbound_retrieval_query(subgoal)
    refined = str(
        (state.subgoal_refined_queries or {}).get(subgoal.id) or ""
    ).strip()
    return refined or query


def _wave_subgoal_result(
    plan: RetrievalPlan,
    state: NavState,
    config: NavConfig,
    subgoal: Subgoal,
    *,
    retrieval_query: str,
    new_chunks: Sequence[Tuple[Any, float]],
    collected_before: Set[str],
    explicit_before: Set[str],
    steps_out: Optional[List[Any]] = None,
) -> SubgoalResult:
    """Build this wave's result from *new* chunks only (never the global pool)."""
    return build_subgoal_result(
        plan,
        state.collected_section_ids,
        config,
        subgoal,
        retrieval_query=retrieval_query,
        new_chunks=new_chunks,
        collected_before=collected_before,
        explicit_collect_ids=state.explicit_collect_ids,
        explicit_before=explicit_before,
        steps_out=steps_out,
    )


@contextmanager
def _relit_map(
    ts: Any,
    state: NavState,
    config: NavConfig,
    *,
    query: str,
    prepared: Optional[Tuple[Dict[str, float], Dict[str, float], List[str]]] = None,
) -> Iterator[None]:
    """Score the shared map against the harvest ``query`` for one call.

    Episode-level ``state.map_scores`` is computed from the original user query.
    Checklist harvests run under a per-subgoal ``retrieval_query``, so the map
    must be re-scored against that string — otherwise the ranking disagrees with
    the query the policy is told to pursue. Scoring failures degrade to the
    episode lighting.
    """
    relit = prepared
    q = (query or "").strip()
    if relit is None and q:
        relit = state.relit_map_cache.get(q)
    if relit is None and q:
        try:
            from .nav_map_scores import relight_map_for_query

            scores, units, highlights = relight_map_for_query(
                ts,
                doc_id=state.doc_id,
                query=q,
                top_k=int(config.collect_top_k),
            )
            if scores:
                relit = (scores, units, highlights)
                state.relit_map_cache[q] = relit
        except Exception:
            relit = None
    if relit is None:
        yield
        return
    saved = (state.map_scores, state.unit_scores, state.highlight_ids)
    state.map_scores, state.unit_scores, state.highlight_ids = relit
    try:
        yield
    finally:
        state.map_scores, state.unit_scores, state.highlight_ids = saved


def _execute_subgoal_harvest_once(
    ts: Any,
    state: NavState,
    config: NavConfig,
    plan: RetrievalPlan,
    subgoal: Subgoal,
    *,
    steps_out: Optional[List[Any]],
    retrieval_query: Optional[str] = None,
    prepared_relight: Optional[
        Tuple[Dict[str, float], Dict[str, float], List[str]]
    ] = None,
) -> Dict[str, Any]:
    """One harvest() call for this subgoal this wave — no internal retry loop.

    Retry / widen / drop / replan authority belongs to ``plan_control`` across
    waves (see ``nav_control.plan_control``), not to this single call.
    """
    rq = retrieval_query or _resolve_subgoal_query(state, subgoal)
    refined = str((state.subgoal_refined_queries or {}).get(subgoal.id) or "").strip()
    _set_focus(state, subgoal, rq)
    # Always enter at namespace/document root; prior dead-ends stay hidden via
    # subgoal_dismissed_section_ids so the next harvest sees siblings instead.
    before_sections = set(state.collected_section_ids)
    before_explicit = set(state.explicit_collect_ids)
    before_len = len(state.collected)
    with _relit_map(
        ts,
        state,
        config,
        query=rq,
        prepared=prepared_relight,
    ):
        harvest_result = _harvest_after_node_filter(
            ts,
            state,
            config,
            subgoal=subgoal,
            query=rq,
            steps_out=steps_out,
        )
    new_chunks = list(state.collected[before_len:])
    signal = _wave_subgoal_result(
        plan,
        state,
        config,
        subgoal,
        retrieval_query=rq,
        new_chunks=new_chunks,
        collected_before=before_sections,
        explicit_before=before_explicit,
        steps_out=steps_out,
    )
    _clear_focus(state)
    return {
        "subgoal_id": subgoal.id,
        "result": signal,
        "new_chunks": new_chunks,
        "harvest": {
            "n_policy_calls": harvest_result.n_policy_calls,
            "visited_section_ids": list(harvest_result.visited_section_ids),
            "max_depth_hit": harvest_result.max_depth_hit,
            "reason": harvest_result.reason,
            "widen_gap": str((state.subgoal_widen_gaps or {}).get(subgoal.id) or ""),
            "refined_query": refined,
        },
    }


def _harvest_after_node_filter(
    ts: Any,
    state: NavState,
    config: NavConfig,
    *,
    subgoal: Subgoal,
    query: str,
    steps_out: Optional[List[Any]],
) -> Any:
    from .nav_harvest import harvest

    if not bool(getattr(config, "enable_node_filter", False)) or not bool(
        getattr(subgoal, "use_node_filter", False)
    ):
        return harvest(
            ts,
            state,
            config,
            subgoal=subgoal,
            entry_scope=None,
            query=query,
            steps_out=steps_out,
        )

    from .nav_scope_filter import run_scope_filter

    doc_ids = list(ts.document_ids() or ())
    if not doc_ids and state.doc_id:
        doc_ids = [str(state.doc_id)]
    seed = None
    raw_seed = list(getattr(subgoal, "node_filter_predicates", None) or [])
    if raw_seed:
        from .nav_scope_filter import parse_node_filter

        seed = parse_node_filter({"predicates": raw_seed})
    outcome = run_scope_filter(
        ts,
        config,
        query=query,
        doc_ids=doc_ids,
        seed_filter=seed,
        steps_out=steps_out,
    )
    if outcome.decision == "collect_all":
        collected = _collect_filtered_sections(
            ts, state, config, subgoal=subgoal, section_ids=outcome.settled_section_ids
        )
        if collected is not None:
            return collected
    if outcome.decision == "scoped_harvest" and outcome.settled_section_ids:
        return harvest(
            ts,
            state,
            config,
            subgoal=subgoal,
            entry_scope=None,
            query=query,
            steps_out=steps_out,
            allowed_section_ids=set(outcome.settled_section_ids),
        )
    return harvest(
        ts,
        state,
        config,
        subgoal=subgoal,
        entry_scope=None,
        query=query,
        steps_out=steps_out,
    )


def _collect_filtered_sections(
    ts: Any,
    state: NavState,
    config: NavConfig,
    *,
    subgoal: Subgoal,
    section_ids: Sequence[str],
) -> Any:
    from .nav_address import is_dispatch_only_node
    from .nav_harvest import HarvestResult
    from .nav_knowhere import is_root_section
    from .nav_navigate import _apply_collect
    from .nav_types import ActionKind, LegalAction

    collectable = [
        sid
        for sid in section_ids
        if str(sid).strip()
        and not is_dispatch_only_node(ts, sid)
        and not is_root_section(ts, sid)
    ]
    if not collectable:
        return None
    actions = [
        LegalAction(
            action_id=f"C{i}",
            kind=ActionKind.COLLECT,
            section_id=sid,
            metadata={"multi": True},
        )
        for i, sid in enumerate(collectable, start=1)
    ]
    primary = actions[0]
    primary.metadata = dict(primary.metadata or {})
    primary.metadata["batch_actions"] = actions
    primary.metadata["confidence_by_section"] = {sid: 1.0 for sid in collectable}
    detail = _apply_collect(ts, state, primary, config)
    new_roots = list(detail.get("collect_section_ids") or [])
    if bool(config.is_checklist):
        for sid in new_roots:
            state.harvested_owner_subgoal[sid] = subgoal.id
    return HarvestResult(
        subgoal_id=subgoal.id,
        new_section_ids=new_roots,
        n_policy_calls=0,
        reason="node_filter_collect_all",
    )


def _apply_plan_control(
    ts: Any,
    state: NavState,
    config: NavConfig,
    *,
    plan: RetrievalPlan,
    outputs: Sequence[Dict[str, Any]],
    by_id: Dict[str, Subgoal],
    steps_out: Optional[List[Any]],
) -> Dict[str, Any]:
    """Apply one wave's plan_control decision; returns a TRACE-friendly detail.

    ``widen`` = leave the subgoal unsettled for the next wave. plan_control says
    what is missing; PLAN turns that into a new retrieval_query, which then
    re-scores the shared map for the next harvest. Prior dead-ends stay
    dismissed. Only ``subgoal_max_attempts`` turns widen into drop.
    """
    from .nav_control import plan_control

    decision = plan_control(ts, state, config, plan=plan, wave_outputs=outputs)
    max_attempts = max(1, int(getattr(config, "subgoal_max_attempts", 2) or 2))

    for item in outputs:
        sid = item["subgoal_id"]
        result: SubgoalResult = item["result"]
        sub_decision = decision.per_subgoal.get(sid)
        has_evidence = int(getattr(result, "chars_used", 0) or 0) > 0
        kind = sub_decision.decision if sub_decision else ("accept" if has_evidence else "widen")
        # Circuit breaker: bound widen loops regardless of plan_control.
        if kind == "widen" and int(state.subgoal_attempt_counts.get(sid, 0)) >= max_attempts:
            kind = "drop"

        if kind == "accept":
            result.satisfied = True
            state.subgoal_results[sid] = asdict(result)
            state.satisfied_subgoal_ids.add(sid)
            state.attempted_subgoal_ids.add(sid)
            state.subgoal_widen_gaps.pop(sid, None)
            state.subgoal_refined_queries.pop(sid, None)
        elif kind == "drop":
            state.dropped_subgoal_ids.add(sid)
            state.attempted_subgoal_ids.add(sid)
            state.subgoal_widen_gaps.pop(sid, None)
            state.subgoal_refined_queries.pop(sid, None)
        else:
            # widen: control's note says what is missing. It is never fed to
            # harvest as text — PLAN reads it against the map and rewrites the
            # query, which is what actually re-ranks the next harvest.
            note = ""
            if sub_decision is not None:
                note = str(sub_decision.note or "").strip()
            if not note:
                note = str(getattr(result, "gap", "") or "").strip()
            if note:
                state.subgoal_widen_gaps[sid] = note
            subgoal = by_id.get(sid)
            if subgoal is not None:
                refined = refine_subgoal_query(
                    ts,
                    state,
                    config,
                    subgoal=subgoal,
                    previous_query=str(
                        state.subgoal_refined_queries.get(sid)
                        or subgoal.retrieval_query
                        or ""
                    ),
                    gap=note,
                    selected_section_ids=list(
                        getattr(result, "explicit_collect_ids", None)
                        or getattr(result, "collected_section_ids", None)
                        or []
                    ),
                )
                if refined:
                    state.subgoal_refined_queries[sid] = refined

    if steps_out is not None:
        from ._compat import AgentStep  # type: ignore

        steps_out.append(
            AgentStep(
                step_idx=len(steps_out) + 1,
                action="plan_control",
                detail=stamp_step_detail({
                    "global": decision.global_action,
                    "reason": decision.reason,
                    "subgoals": {
                        sid: {"decision": d.decision, "note": d.note}
                        for sid, d in decision.per_subgoal.items()
                    },
                }),
            )
        )
    return {
        "global": decision.global_action,
        "replan": decision.global_action == "replan",
        "done": decision.global_action == "done",
        "reason": decision.reason,
    }


def execute_plan(
    ts: Any,
    state: NavState,
    config: NavConfig,
    *,
    steps_out: Optional[List[Any]] = None,
    episode_query: str = "",
) -> Dict[str, Any]:
    """Run dependency waves until idle or ``max_waves``."""
    from .nav_agent import AgentStep

    plan = state.retrieval_plan
    if plan is None or not getattr(plan, "subgoals", None):
        # Retired: no multi-step navigate fallback. Planning always yields a
        # subgoal (fallback_plan on failure); an empty plan here is a bug.
        _logger.warning("execute_plan called with empty retrieval_plan; skipping wave")
        return {"waves": [], "results": {}}

    max_waves = int(getattr(config, "max_waves", 0) or 0)
    # Checklist mode always harvests + plan_controls.
    wave_idx = 0
    summary: Dict[str, Any] = {"waves": [], "results": {}}
    episode_done = False

    while True:
        if episode_done:
            break
        if max_waves > 0 and wave_idx >= max_waves:
            break
        ready = ready_subgoal_ids(
            plan,
            satisfied=set(state.satisfied_subgoal_ids),
            attempted=set(state.attempted_subgoal_ids),
            dropped=set(state.dropped_subgoal_ids),
        )
        if not ready:
            break
        wave_idx += 1
        wave_detail: Dict[str, Any] = {
            "wave": wave_idx,
            "ready": list(ready),
            "subgoal_results": [],
        }

        by_id = {s.id: s for s in plan.subgoals}
        outputs: List[Dict[str, Any]] = []
        query_by_subgoal = {
            sid: _resolve_subgoal_query(state, by_id[sid]) for sid in ready
        }
        prepared_relights: Dict[
            str,
            Tuple[Dict[str, float], Dict[str, float], List[str]],
        ] = {}
        try:
            from .nav_map_scores import relight_maps_for_queries

            prepared_relights = relight_maps_for_queries(
                ts,
                doc_id=state.doc_id,
                queries=list(query_by_subgoal.values()),
                top_k=int(config.collect_top_k),
            )
        except Exception:
            prepared_relights = {}

        def _run_one(sid: str, working_state: NavState, out_steps: Optional[List[Any]]) -> Dict[str, Any]:
            query = query_by_subgoal[sid]
            prepared = prepared_relights.get(query)
            if prepared is not None and not prepared[0]:
                prepared = None
            return _execute_subgoal_harvest_once(
                ts,
                working_state,
                config,
                plan,
                by_id[sid],
                steps_out=out_steps,
                retrieval_query=query,
                prepared_relight=prepared,
            )

        # Serial wave execution (parallel fan-out retired with ThreadPoolExecutor).
        wave_started = time.perf_counter()
        for sid in ready:
            subgoal_started = time.perf_counter()
            try:
                outputs.append(_run_one(sid, state, steps_out))
            finally:
                _logger.info(
                    "retrieval mapnav harvest subgoal=%s wave=%d seconds=%.3f",
                    sid,
                    wave_idx,
                    time.perf_counter() - subgoal_started,
                )

        # Bookkeeping shared by both decision paths.
        for item in outputs:
            sid = item["subgoal_id"]
            result: SubgoalResult = item["result"]
            state.subgoal_results[sid] = asdict(result)
            wave_detail["subgoal_results"].append(
                {
                    "subgoal_id": sid,
                    "chars_used": result.chars_used,
                    "gap": result.gap,
                    "extracted": dict(result.extracted or {}),
                }
            )
            summary["results"][sid] = asdict(result)
            if result.extracted:
                state.slot_bindings = apply_bindings_from_result(
                    state.slot_bindings, by_id[sid], result.extracted
                )
            state.subgoal_attempt_counts[sid] = int(
                state.subgoal_attempt_counts.get(sid, 0)
            ) + 1

        control_started = time.perf_counter()
        try:
            control_detail = _apply_plan_control(
                ts, state, config, plan=plan, outputs=outputs, by_id=by_id, steps_out=steps_out
            )
        finally:
            _logger.info(
                "retrieval mapnav plan_control wave=%d seconds=%.3f",
                wave_idx,
                time.perf_counter() - control_started,
            )
        wave_detail["plan_control"] = control_detail
        replan_requested = bool(control_detail.get("replan"))
        if control_detail.get("done"):
            episode_done = True

        if steps_out is not None:
            steps_out.append(
                AgentStep(
                    step_idx=len(steps_out) + 1,
                    action="plan_wave",
                    detail=stamp_step_detail(wave_detail),
                )
            )
        summary["waves"].append(wave_detail)
        _logger.info(
            "retrieval mapnav wave=%d ready=%d seconds=%.3f",
            wave_idx,
            len(ready),
            time.perf_counter() - wave_started,
        )

        if replan_requested:
            cap = int(getattr(config, "max_replans", 0) or 0)
            if cap > 0 and int(state.replan_count) < cap:
                state.replan_count += 1
                t0 = time.perf_counter()
                new_plan = plan_query(ts, state, config)
                state.retrieval_plan = new_plan
                plan = new_plan
                # A regenerated plan gets fresh subgoal ids (s1, s2, ... again),
                # so per-id bookkeeping (satisfied/attempted/dropped/widen gaps,
                # qualified "sX.slot" bindings) cannot be safely carried over —
                # those ids now mean something else. What IS safe and worth
                # keeping is unqualified slot bindings (plain fact values) and
                # every chunk already in state.collected.
                state.satisfied_subgoal_ids = set()
                state.attempted_subgoal_ids = set()
                state.dropped_subgoal_ids = set()
                state.subgoal_results = {}
                state.subgoal_widen_gaps = {}
                state.subgoal_refined_queries = {}
                state.subgoal_attempt_counts = {}
                state.subgoal_dismissed_section_ids = {}
                state.slot_bindings = {
                    k: v for k, v in state.slot_bindings.items() if "." not in k
                }
                if steps_out is not None:
                    steps_out.append(
                        AgentStep(
                            step_idx=len(steps_out) + 1,
                            action="replan",
                            detail=stamp_step_detail({
                                "replan_count": state.replan_count,
                                "n_subgoals": len(new_plan.subgoals),
                                "fallback": bool(new_plan.fallback),
                                "seconds": time.perf_counter() - t0,
                            }),
                        )
                    )
                continue
            # Cap reached: stop requesting further replans.
            replan_requested = False

    _clear_focus(state)
    summary["n_waves"] = wave_idx
    summary["satisfied"] = sorted(state.satisfied_subgoal_ids)
    summary["attempted"] = sorted(state.attempted_subgoal_ids)
    summary["dropped"] = sorted(state.dropped_subgoal_ids)
    return summary
