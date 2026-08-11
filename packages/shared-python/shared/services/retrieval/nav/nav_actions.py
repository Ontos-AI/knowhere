from __future__ import annotations

import os
from typing import Any, List, Optional

from .nav_address import is_dispatch_only_node
from .nav_knowhere import is_root_section
from .nav_projection import format_harvested_tag, format_hit_tag
from .nav_types import (
    ActionKind,
    LegalAction,
    NavConfig,
    NavState,
    Projection,
    SectionView,
)


def _env_enabled(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _budget_mode(step_idx: int, config: NavConfig, *, max_steps: Optional[int] = None) -> str:
    episode_steps = int(max_steps if max_steps is not None else config.max_steps)
    remaining = max(0, episode_steps - step_idx)
    if remaining <= config.critical_remaining_steps:
        return "critical"
    if remaining <= config.tight_remaining_steps:
        return "tight"
    return "normal"


def build_legal_actions(
    state: NavState,
    projection: Projection,
    *,
    step_idx: int,
    config: NavConfig,
    depth: int = 0,
    max_steps: Optional[int] = None,
    ts: Any = None,
) -> List[LegalAction]:
    """Every visible node is actionable: COLLECT + DISPATCH (when allowed) + FINISH.

    No action-space top-K: visibility is governed only by map display budget folding.
    FINISH is always available; the LLM decides when to exit the current scope.
    DISPATCH never targets the current scope root (no self-dispatch loop).
    Document / namespace nodes are DISPATCH-only (level registry via ``ts``).
    """
    episode_steps = int(max_steps if max_steps is not None else config.max_steps)
    mode = _budget_mode(step_idx, config, max_steps=episode_steps)
    actions: List[LegalAction] = []
    filter_collected = _env_enabled("NAV_FILTER_COLLECTED_SECTIONS")
    collected_sids = set(state.collected_section_ids) | {
        str(h.get("section_id") or "").strip()
        for h in state.action_history
        if h.get("kind") == "collect" and int(h.get("n_added", 0) or 0) > 0
    }
    blocked = set(state.blocked_collect_section_ids)
    map_scores = dict(getattr(state, "map_scores", {}) or {})
    unit_scores = dict(getattr(state, "unit_scores", {}) or {})
    highlight_set = set(projection.highlight_ids or state.highlight_ids or [])
    scope_id = str(state.current_scope or projection.scope or "").strip()

    # DISPATCH only at depth 0 unless recursive dispatch is enabled and under max depth.
    can_dispatch = depth == 0 or (
        bool(config.enable_recursive_dispatch) and depth < int(config.max_dispatch_depth)
    )

    rows = list(projection.tree_sections) or list(projection.visible_sections)
    collect_i = 1
    dispatch_i = 1

    def view_score(view: SectionView) -> float:
        score = float(view.score)
        if score <= 0.0:
            score = float(map_scores.get(view.section_id, 0.0) or 0.0)
        if score <= 0.0:
            score = float(unit_scores.get(view.section_id, 0.0) or 0.0)
            self_key = f"{view.section_id}__self"
            if self_key in unit_scores:
                score = max(score, float(unit_scores[self_key] or 0.0))
        return score

    for view in rows:
        sid = view.section_id
        label = view.title or view.preview or sid
        score = view_score(view)
        is_hit = sid in highlight_set or bool(view.is_highlight)

        collect_blocked = filter_collected and sid in (
            collected_sids | blocked
        )
        if is_dispatch_only_node(ts, sid) or is_root_section(ts, sid):
            collect_blocked = True
        if not collect_blocked:
            actions.append(
                LegalAction(
                    action_id=f"C{collect_i}",
                    kind=ActionKind.COLLECT,
                    section_id=sid,
                    label=label,
                    score=score,
                    metadata={
                        "map_id": view.map_id,
                        "n_chunks": view.n_chunks,
                        "highlight": is_hit,
                        "multi": True,
                    },
                )
            )
            collect_i += 1

        if (
            can_dispatch
            and view.has_children
            and sid not in collected_sids
            and sid != scope_id
            and mode != "critical"
        ):
            actions.append(
                LegalAction(
                    action_id=f"D{dispatch_i}",
                    kind=ActionKind.DISPATCH,
                    section_id=sid,
                    label=label,
                    score=score,
                    metadata={
                        "map_id": view.map_id,
                        "highlight": is_hit,
                        "multi": True,
                    },
                )
            )
            dispatch_i += 1

    actions.append(
        LegalAction(
            action_id="F1",
            kind=ActionKind.FINISH,
            label="finish navigation and pack final evidence budget",
        )
    )
    return actions


def format_actionable_map_observation(
    projection: Projection,
    actions: List[LegalAction],
    *,
    inline_summary: bool = False,
) -> str:
    """Tree observation: each node line carries its legal action IDs."""
    by_sid: dict[str, list[LegalAction]] = {}
    global_actions: List[LegalAction] = []
    for act in actions:
        if act.kind == ActionKind.FINISH or not act.section_id:
            global_actions.append(act)
            continue
        by_sid.setdefault(str(act.section_id), []).append(act)

    kind_label = {
        ActionKind.COLLECT: "collect",
        ActionKind.DISPATCH: "dispatch",
    }

    def node_actions(sid: str) -> str:
        acts = by_sid.get(sid) or []
        if not acts:
            return "none"
        order = {ActionKind.COLLECT: 0, ActionKind.DISPATCH: 1}
        acts = sorted(acts, key=lambda a: (order.get(a.kind, 9), a.action_id))
        return ", ".join(
            f"{kind_label.get(a.kind, a.kind.value)}={a.action_id}" for a in acts
        )

    rows = list(projection.tree_sections) or list(projection.visible_sections)
    lines = [
        f"doc_id={projection.doc_id}",
        f"scope={projection.scope or '<document-root>'}",
        "Each visible section appears once. Choose action IDs attached to the relevant line.",
    ]
    for view in rows:
        indent = "  " * max(0, int(view.depth_from_scope))
        leaf_tag = " [Leaf]" if not view.has_children else ""
        hit_tag = format_hit_tag(is_highlight=bool(view.is_highlight))
        harvested_tag = format_harvested_tag(getattr(view, "harvested_by", "") or "")
        map_id = view.map_id or "?"
        meta = f"({view.n_chunks} chunks)"
        lines.append(
            f"{indent}[{map_id}] {view.title or view.section_id} {meta}"
            f"{leaf_tag}{hit_tag}{harvested_tag} actions: {node_actions(view.section_id)}"
        )
        if inline_summary and view.summary:
            lines.append(f"{indent}    summary: {view.summary}")

    if global_actions:
        lines.append("")
        lines.append("Global actions:")
        for act in global_actions:
            if act.kind == ActionKind.FINISH:
                lines.append(f"  finish={act.action_id}")
            else:
                lines.append(f"  {act.action_id} ({act.kind.value})")
    # SEARCH_* is harvest ``search_assets`` (nav_assets), not a map-row action.
    return "\n".join(lines)


def action_by_id(actions: List[LegalAction], action_id: Optional[str]) -> Optional[LegalAction]:
    aid = (action_id or "").strip().upper()
    if not aid:
        return None
    for a in actions:
        if a.action_id.upper() == aid:
            return a
    return None


def actions_by_ids(actions: List[LegalAction], ids: List[str]) -> List[LegalAction]:
    """Resolve a multi-id selection (COLLECT / DISPATCH), preserving request order."""
    by_id = {a.action_id.upper(): a for a in actions}
    out: List[LegalAction] = []
    seen: set[str] = set()
    for raw in ids:
        aid = (raw or "").strip().upper()
        if not aid or aid in seen:
            continue
        act = by_id.get(aid)
        if act is not None:
            out.append(act)
            seen.add(aid)
    return out
