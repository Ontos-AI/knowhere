from __future__ import annotations

import logging
import os
import time
from typing import Any, List, Optional, Sequence, Tuple

from ._compat import AgentStep, EpisodeResult
from ._compat import compose_answer_llm
from ._compat import HierarchicalTools
from ._compat import Chunk
from ._compat import (
    Refusal,
    ToolSpace,
)
from .nav_address import (
    is_dispatch_only_node,
    owner_document,
    uses_document_nodes,
)
from .nav_compose import (
    evidence_owner_section_id,
    pack_nav_evidence,
    unit_score_for_evidence_chunk,
)
from .nav_map_scores import (
    compute_corpus_map_and_unit_scores,
    compute_map_and_unit_scores,
    select_map_highlights,
)
from .nav_types import (
    LegalAction,
    NavConfig,
    NavState,
)

# Back-compat aliases for tests / callers.
_evidence_owner_section_id = evidence_owner_section_id
_unit_score_for_evidence_chunk = unit_score_for_evidence_chunk
_logger = logging.getLogger(__name__)


def _chunks_to_retrieved_nodes(chunks: List[Chunk]) -> List[str]:
    """Stable unit ids: prefer chunk/node id (Knowhere ``chunk_id``)."""
    seen: set[str] = set()
    out: List[str] = []
    for c in chunks:
        node = str(getattr(c, "node_id", "") or "").strip()
        if not node:
            node = str(getattr(c, "section_id", "") or "").strip()
        if not node or node in seen:
            continue
        seen.add(node)
        out.append(node)
    return out


def _dedupe_scored(scored: List[Tuple[Chunk, float]]) -> List[Tuple[Chunk, float]]:
    from .nav_compose import dedupe_scored

    return dedupe_scored(scored)


def _add_scored(state: NavState, scored: List[Tuple[Chunk, float]]) -> int:
    added = 0
    for c, score in scored:
        if c.node_id in state.collected_ids:
            continue
        state.collected_ids.add(c.node_id)
        state.collected.append((c, float(score)))
        added += 1
    return added


def _resolve_action_doc_id(
    action_or_sid: Any,
    state: NavState,
    ts: Any = None,
) -> str:
    """Owning document_id for hydrate; never a corpus/namespace sentinel."""
    if hasattr(action_or_sid, "section_id"):
        sid = str(getattr(action_or_sid, "section_id", "") or "").strip()
    else:
        sid = str(action_or_sid or "").strip()
    resolved = owner_document(ts, sid, "") if ts is not None else ""
    if resolved:
        return resolved
    if state.doc_id:
        return state.doc_id
    return ""


def _purge_descendant_evidence(
    ts: ToolSpace,
    state: NavState,
    parent_sid: str,
) -> int:
    """Drop standalone evidence owned by proper descendants of parent_sid.

    When COLLECT parent after COLLECT child (e.g. L93 then L92), child chunks
    are absorbed into the parent hydrate and must not keep a separate bag slot.
    """
    sid = str(parent_sid or "").strip()
    if not sid:
        return 0
    doc = _resolve_action_doc_id(sid, state, ts)
    relations = getattr(ts, "section_relation_ids", None)
    if callable(relations):
        _anc, descendants = relations(sid, doc)
    else:
        descendants = _section_and_descendants(ts, sid, doc)
    descendants = {str(x).strip() for x in (descendants or set()) if str(x).strip()}
    descendants.discard(sid)
    if not descendants:
        return 0

    kept: List[Tuple[Chunk, float]] = []
    removed = 0
    for chunk, score in list(state.collected):
        owner = _evidence_owner_section_id(chunk)
        if owner in descendants:
            state.collected_ids.discard(chunk.node_id)
            removed += 1
            continue
        kept.append((chunk, score))
    state.collected = kept
    return removed


def _env_enabled(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _line_order(pool: List[Chunk]) -> List[Chunk]:
    return sorted(pool, key=lambda c: (min(c.line_ids or (10**9,)), c.node_id))


def _collect_subtree(ts: ToolSpace, action: LegalAction, state: NavState, config: NavConfig) -> List[Tuple[Chunk, float]]:
    """Hydrate ``section_id ∪ descendants`` in document order.

    No collect-time top-K / unit-score truncation — final size is controlled by
    compose ``budget_chars`` progressive trim (MAP-NAV subtree collect).
    """
    sid = action.section_id
    if not sid:
        return []
    if is_dispatch_only_node(ts, sid):
        return []
    doc = _resolve_action_doc_id(action, state, ts)
    if not doc:
        return []
    materialize = getattr(ts, "_materialize_leaf_path_chunks", None)
    if callable(materialize):
        pool = list(materialize(sid, doc))
        if pool:
            return _collect_in_doc_order(pool, config)
    rc = ts.read_chunks(sid, state.query, doc_id=doc, k=int(config.collect_k))
    if isinstance(rc, Refusal):
        state.refusal_events.append(
            {
                "tool": "collect",
                "section_id": sid,
                "status": rc.status,
                "message": rc.message,
                "available_sections": list(rc.available_sections),
            }
        )
        return []
    return [(h.chunk, float(h.score) + float(config.read_score_bonus)) for h in rc]


def _mark_collected_branch(
    ts: ToolSpace, action: LegalAction, state: NavState, added: int
) -> dict[str, Any]:
    """On successful COLLECT: mark sid ∪ descendants as collected (removed from map).

    Replaces the old covered/collected split — one set only.
    """
    sid = str(action.section_id or "").strip()
    if not sid or not _env_enabled("NAV_FILTER_COLLECTED_SECTIONS"):
        return {}
    doc = _resolve_action_doc_id(action, state, ts)
    materialize = getattr(ts, "_materialize_leaf_path_chunks", None)
    relations = getattr(ts, "section_relation_ids", None)
    pool = list(materialize(sid, doc)) if callable(materialize) and doc else []
    if callable(relations) and doc:
        ancestors, descendants = relations(sid, doc)
    else:
        ancestors, descendants = set(), _section_and_descendants(ts, sid, doc)
    descendants = {str(x).strip() for x in (descendants or set()) if str(x).strip()}
    descendants.add(sid)

    is_full = bool(pool) and all(chunk.node_id in state.collected_ids for chunk in pool)
    if added > 0:
        state.collected_section_ids.update(descendants)
        if len(pool) > 1:
            state.blocked_collect_section_ids.update(ancestors)
    return {
        "collect_full": is_full,
        "branch_selected": added > 0,
        "n_collected_sections": len(state.collected_section_ids),
        "n_blocked_ancestor_collects": len(state.blocked_collect_section_ids),
    }


# Back-compat name used by older tests; prefer _mark_collected_branch.
_update_collect_coverage = _mark_collected_branch


def _collect_in_doc_order(
    pool: List[Chunk],
    config: NavConfig,
) -> List[Tuple[Chunk, float]]:
    """Hydrate the complete branch in document order; COMPOSE truncates later."""
    if not pool:
        return []
    base = float(config.read_score_bonus)
    return [(chunk, base) for chunk in _line_order(pool)]


def _direct_child_ids(ts: ToolSpace, section_id: str, doc_id: str) -> List[str]:
    sid = (section_id or "").strip()
    if not sid:
        return []
    children_fn = getattr(ts, "_children_for_section_path", None)
    rows: List[Any] = []
    if callable(children_fn):
        try:
            rows = list(children_fn(sid, doc_id, limit=100000) or [])
        except Exception:
            rows = []
    if not rows:
        try:
            st = ts.get_structure(sid)
            rows = list(st.get("children") or [])
        except Exception:
            return []
    out: List[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        child_id = str(row.get("section_id") or "").strip()
        if child_id and child_id not in seen:
            seen.add(child_id)
            out.append(child_id)
    return out


def _section_and_descendants(ts: ToolSpace, section_id: str, doc_id: str) -> set[str]:
    """Return {section_id} ∪ descendants (best-effort)."""
    sid = (section_id or "").strip()
    if not sid:
        return set()
    relations = getattr(ts, "section_relation_ids", None)
    if callable(relations):
        try:
            _anc, desc = relations(sid, doc_id)
            out = {str(x).strip() for x in (desc or set()) if str(x).strip()}
            out.add(sid)
            return out
        except Exception:
            pass
    out: set[str] = {sid}
    stack = [sid]
    while stack:
        cur = stack.pop()
        for child in _direct_child_ids(ts, cur, doc_id):
            if child not in out:
                out.add(child)
                stack.append(child)
    return out


def _episode_stop_reason(steps: Sequence[Any]) -> str:
    """Code-derived episode stop label from budget state / step meta (not LLM)."""
    from .nav_token_budget import nav_token_budget_exhausted

    if nav_token_budget_exhausted():
        return "token_limit"
    for step in steps:
        detail = getattr(step, "detail", None) or {}
        if not isinstance(detail, dict):
            continue
        sr = str(detail.get("stop_reason") or "").strip()
        if sr:
            return sr
        if str(detail.get("reason") or "").strip() == "token_limit":
            return "token_limit"
    return "completed"


def run_nav_episode(
    tools: Optional[HierarchicalTools],
    query: str,
    *,
    doc_id: Optional[str] = None,
    corpus_doc_ids: Optional[Sequence[str]] = None,
    budget_chars: int,
    task_type: str = "unknown",
    compose_format_constraints: str = "",
    compose_answer: bool = True,
    policy: str = "rule",
    config: Optional[NavConfig] = None,
    toolspace: Optional[Any] = None,
) -> EpisodeResult:
    from .nav_llm import nav_llm_runtime
    from .nav_token_budget import nav_token_episode

    cfg = config or NavConfig(policy="llm")
    with nav_token_episode(token_limit=int(getattr(cfg, "token_limit", 0) or 0)):
        with nav_llm_runtime(
            planner_thinking=str(getattr(cfg, "planner_thinking", "") or ""),
            planner_think_max_tokens=int(
                getattr(cfg, "planner_think_max_tokens", 0) or 0
            ),
        ):
            return _run_nav_episode_body(
                tools,
                query,
                doc_id=doc_id,
                corpus_doc_ids=corpus_doc_ids,
                budget_chars=budget_chars,
                task_type=task_type,
                compose_format_constraints=compose_format_constraints,
                compose_answer=compose_answer,
                policy=policy,
                config=cfg,
                toolspace=toolspace,
            )


def _run_nav_episode_body(
    tools: Optional[HierarchicalTools],
    query: str,
    *,
    doc_id: Optional[str] = None,
    corpus_doc_ids: Optional[Sequence[str]] = None,
    budget_chars: int,
    task_type: str = "unknown",
    compose_format_constraints: str = "",
    compose_answer: bool = True,
    policy: str = "rule",
    config: Optional[NavConfig] = None,
    toolspace: Optional[Any] = None,
) -> EpisodeResult:
    from .nav_token_budget import stamp_step_detail

    corpus_ids = [
        str(d).strip()
        for d in (corpus_doc_ids or [])
        if str(d).strip()
    ]
    episode_doc = str(doc_id or "").strip()
    if not episode_doc and not corpus_ids:
        raise ValueError(
            "Nav Agent requires doc_id or non-empty corpus_doc_ids "
            "(eval entry points always pass corpus_doc_ids for task_corpus)"
        )
    from ._compat import load_llm_env, require_llm_env  # type: ignore

    load_llm_env()
    require_llm_env(context="Nav Agent")
    cfg = config or NavConfig(policy="llm")
    if cfg.map_mode and cfg.llm_max_tokens < 256:
        cfg.llm_max_tokens = 256
    nav_policy = (policy or cfg.policy or "llm").strip().lower()
    if nav_policy != "llm":
        raise ValueError(
            f"Nav Agent 仅支持 llm 策略（须配置 DS_KEY 或 OPENAI_API_KEY）；"
            f"收到 policy={policy!r}。"
            "请设置 --nav-policy llm 或删除 NAV_POLICY=rule。"
        )
    cfg.policy = "llm"
    # Tie the large-scope title-only threshold to the real evidence budget
    # (budget_chars x mult): a scope whose full summary map would dwarf the final
    # evidence budget is shown title-only, nudging DISPATCH over broad COLLECT.
    mult = float(getattr(cfg, "scope_inline_summary_budget_mult", 0.0) or 0.0)
    if mult > 0.0 and int(budget_chars) > 0:
        cfg.scope_inline_summary_char_limit = max(1, int(budget_chars * mult))
    retrieval_t0 = time.perf_counter()
    if toolspace is not None:
        ts = toolspace
    elif tools is not None:
        ts = ToolSpace(tools, corpus_doc_ids=corpus_ids or None)
    else:
        raise ValueError("Nav Agent requires either tools or an injected toolspace")

    # Namespace / multi-doc: document ids are map nodes; empty scope is the root.
    namespace_mode = bool(corpus_ids) and uses_document_nodes(ts)
    if namespace_mode:
        if not corpus_ids:
            corpus_ids = list(ts.document_ids())
        episode_doc = ""

    state = NavState(doc_id=episode_doc, query=query, task_type=task_type)
    steps: List[AgentStep] = []
    map_started = time.perf_counter()
    if namespace_mode:
        section_ids = list(ts.sections_for_doc(""))
        state.map_scores, state.unit_scores = compute_corpus_map_and_unit_scores(
            ts, doc_ids=corpus_ids, query=query
        )
    else:
        section_ids = ts.sections_for_doc(episode_doc)
        state.map_scores, state.unit_scores = compute_map_and_unit_scores(
            ts, doc_id=episode_doc, query=query, root_ids=section_ids
        )
    _logger.info(
        "retrieval mapnav phase=map_scoring seconds=%.3f documents=%d sections=%d",
        time.perf_counter() - map_started,
        len(corpus_ids),
        len(section_ids),
    )
    state.highlight_ids = select_map_highlights(
        state.unit_scores, k=int(cfg.collect_top_k)
    )

    from .nav_plan import plan_query

    plan_t0 = time.perf_counter()
    retrieval_plan = plan_query(ts, state, cfg)
    state.retrieval_plan = retrieval_plan
    steps.append(
        AgentStep(
            step_idx=len(steps) + 1,
            action="query_plan",
            detail=stamp_step_detail({
                "fallback": bool(retrieval_plan.fallback),
                "n_subgoals": len(retrieval_plan.subgoals),
                "reason": retrieval_plan.reason,
                "plan": retrieval_plan.to_dict(),
                "planning_map_char_limit": int(
                    getattr(cfg, "planning_map_char_limit", 0) or cfg.map_char_limit
                ),
                "seconds": time.perf_counter() - plan_t0,
            }, t0=plan_t0),
        )
    )
    _logger.info(
        "retrieval mapnav phase=planner seconds=%.3f subgoals=%d",
        time.perf_counter() - plan_t0,
        len(retrieval_plan.subgoals),
    )

    # Checklist: wave orchestration. Every episode runs plan + harvest + control.
    if state.retrieval_plan is None:
        from .nav_plan import fallback_plan

        state.retrieval_plan = fallback_plan(query, reason="missing_plan")
    from .nav_orchestrate import execute_plan

    orch_t0 = time.perf_counter()
    orch_detail = execute_plan(
        ts, state, cfg, steps_out=steps, episode_query=query
    )
    orch_detail = dict(orch_detail or {})
    orch_detail["seconds"] = time.perf_counter() - orch_t0
    steps.append(
        AgentStep(
            step_idx=len(steps) + 1,
            action="plan_orchestrate",
            detail=stamp_step_detail(orch_detail, t0=orch_t0),
        )
    )
    _logger.info(
        "retrieval mapnav phase=orchestration seconds=%.3f waves=%d",
        time.perf_counter() - orch_t0,
        len(orch_detail.get("waves", [])),
    )

    evidence_started = time.perf_counter()
    fill = pack_nav_evidence(
        _dedupe_scored(list(state.collected)),
        ts,
        state,
        cfg,
        budget_chars=budget_chars,
    )
    _logger.info(
        "retrieval mapnav phase=evidence_pack seconds=%.3f chunks=%d",
        time.perf_counter() - evidence_started,
        len(fill.kept_chunks),
    )
    scored_chunks = list(fill.scored_chunks)
    retrieval_seconds = time.perf_counter() - retrieval_t0
    composed = ""
    compose_seconds = 0.0
    if compose_answer:
        compose_t0 = time.perf_counter()
        max_ans = min(1024, max(256, int(budget_chars)))
        extra_mh_constraint = ""
        if (task_type or "").strip().lower() == "multi_hop":
            extra_mh_constraint = (
                "multi_hop 约束：fact_1 与 fact_2 必须分别覆盖两跳信息，"
                "final_answer 必须整合二者，任一缺失视为不完整。"
            )
        fc = compose_format_constraints
        if extra_mh_constraint:
            fc = (f"{fc}\n{extra_mh_constraint}" if fc else extra_mh_constraint)
        composed = compose_answer_llm(
            query,
            task_type=task_type or "niche_fact",
            evidence_text=fill.evidence_text or "",
            max_answer_chars=max_ans,
            budget_chars=int(budget_chars),
            format_constraints=fc,
        )
        compose_seconds = time.perf_counter() - compose_t0
        steps.append(
            AgentStep(
                step_idx=len(steps) + 1,
                action="compose_answer",
                detail=stamp_step_detail({
                    "evidence_chars": fill.evidence_chars_actual,
                    "n_chunks_kept": fill.n_chunks_kept,
                    "truncated_last": fill.truncated_last,
                }, t0=compose_t0),
            )
        )

    return EpisodeResult(
        representation=f"hierarchical_nav_{cfg.policy}",
        steps=steps,
        scored_chunks=scored_chunks,
        kept_chunks=fill.kept_chunks,
        evidence_text=fill.evidence_text,
        evidence_chars_actual=fill.evidence_chars_actual,
        retrieved_nodes=_chunks_to_retrieved_nodes(list(fill.kept_chunks)),
        composed_answer=composed,
        section_ids=list(section_ids),
        trajectory_length=len(steps),
        truncated_last=fill.truncated_last,
        refusal_events=list(state.refusal_events),
        phase_timings={
            "retrieval_framework_seconds": retrieval_seconds,
            "compose_seconds": compose_seconds,
            "online_response_seconds": retrieval_seconds + compose_seconds,
        },
        stop_reason=_episode_stop_reason(steps),
    )
