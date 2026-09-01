from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Tuple

NavMode = Literal["checklist"]


class ActionKind(str, Enum):
    COLLECT = "collect"
    DISPATCH = "dispatch"
    FINISH = "finish"


def map_mode_enabled(config: "NavConfig | None" = None) -> bool:
    """True when map-first observation/actions are active.

    When ``config`` is provided it is authoritative (Knowhere production binds
    ``map_mode`` on ``NavConfig``). Env ``NAV_MAP_MODE`` is only for EXP scripts
    that call without a config.
    """
    if config is not None:
        return bool(getattr(config, "map_mode", False))
    return os.environ.get("NAV_MAP_MODE", "0").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "",
    }


@dataclass
class NavConfig:
    projection_depth: int = 2
    projection_child_limit: int = 8
    projection_char_limit: int = 8000
    summary_chars: int = 120
    max_steps: int = 8
    collect_k: int = 64
    search_k: int = 40
    collect_top_k: int = 6  # rescue-K for highlights (not action quota)
    read_score_bonus: float = 10.0
    policy: str = "rule"
    # Empty → resolve_nav_model falls back to NAV_* env (EXP scripts).
    # Knowhere build_nav_config sets concrete model names.
    llm_model: str = ""
    llm_temperature: float = 0.0
    llm_max_tokens: int = 256
    critical_remaining_steps: int = 1
    tight_remaining_steps: int = 2
    # Map-first mode (also gated by NAV_MAP_MODE env).
    map_mode: bool = False
    map_char_limit: int = 5000  # display budget (fold threshold); only hard display limit
    map_children_limit: int = 10000
    # Recursive dispatch.
    enable_recursive_dispatch: bool = True
    max_dispatch_depth: int = 3
    subagent_model: str = ""
    # Scoped maps whose estimated (with-summary) size exceeds this threshold drop
    # inline summaries (title-only), nudging the agent to DISPATCH deeper rather
    # than broadly COLLECT the whole parent. Default 1500 == evidence budget 500 x3.
    # run_nav_episode re-derives it from the episode's evidence budget x mult below.
    scope_inline_summary_char_limit: int = 1500
    scope_inline_summary_budget_mult: float = 3.0
    # COMPOSE child score = own_unit + compose_confidence_weight * collect_confidence
    # (see nav_compose._child_final_score); drives group_key / within-group rank.
    compose_confidence_weight: float = 0.5
    # Product mode: checklist = plan+harvest+control.
    mode: NavMode = "checklist"
    # Display budget for the one-shot planning map; executor still uses map_char_limit.
    # 0 = reuse map_char_limit.
    planning_map_char_limit: int = 10000
    # Soft prompt guidance only when > 0; never silently truncates a valid plan.
    planner_max_subgoals: int = 0
    planner_model: str = ""
    # "", "enabled", or "disabled". Empty → NAV_PLANNER_THINKING / default off.
    planner_thinking: str = ""
    planner_think_max_tokens: int = 0
    # 0 → RETRIEVAL_NAV_TOKEN_LIMIT env / default 100000.
    token_limit: int = 0
    # Plan JSON is larger than a single harvest action; give the planner more room.
    planner_llm_max_tokens: int = 1024
    # Harvest multi-id JSON (collect_ids + per-id confidence) needs more than a
    # bare 256 — capped completion truncates mid-object and parses as empty.
    harvest_llm_max_tokens: int = 1024
    # Checklist: harvest cycles per subgoal before drop. Min 1.
    subgoal_max_attempts: int = 2
    # Checklist: 0 = never replan; otherwise hard cap on structural replans.
    # Default 1 so shared-checklist probe/harness match without silent overrides.
    max_replans: int = 1
    # Checklist: 0 = no extra wave cap (stop when no ready subgoals).
    max_waves: int = 0
    # Structural recursion depth cap for harvest() (checklist mode).
    max_harvest_depth: int = 3
    # Retired: plan_control now shows full prebuilt section summaries (already
    # head/tail clipped at summary-build time), not a raw-evidence char cut.
    plan_control_digest_chars: int = 600
    # WHERE node filter (pre-harvest). Off until orchestrate enables a subgoal.
    enable_node_filter: bool = False
    filter_max_rounds: int = 3
    filter_min_hits: int = 1
    filter_max_hits: int = 40

    @property
    def is_checklist(self) -> bool:
        return str(self.mode or "").strip().lower() == "checklist"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NavConfig":
        flat = dict(data)
        budget_modes = flat.pop("budget_modes", {}) or {}
        if isinstance(budget_modes, dict):
            flat["critical_remaining_steps"] = int(
                budget_modes.get("critical_remaining_steps", cls.critical_remaining_steps)
            )
            flat["tight_remaining_steps"] = int(
                budget_modes.get("tight_remaining_steps", cls.tight_remaining_steps)
            )
        # Retired product flags (dropped after the navigate loop was removed).
        for dead in (
            "expand_top_k",
            "map_peek_top_k",
            "map_jump_top_k",
            "peek_content_fanout",
            "peek_content_chars",
            "map_collapse_min_score",
            "dispatch_group_size",
            "dispatch_max_workers",
            "enable_contract_verify",
            "enable_per_subgoal_illumination",
            "enable_goal_conditioned_folding",
            "enable_subgoal_budget_ledger",
            "subgoal_budget_floor_frac",
            "enable_anchor_entry",
            "enable_settle_group_rank",
            "enable_external_rerank",
            "compose_preview_snippet_chars",
            "compose_preview_max_children",
            "compose_packing_mode",
            "compose_coverage_budget_frac",
            "compose_snippet_chars",
            "enable_query_planning",
            "enable_plan_orchestration",
            "enable_slot_extract",
            "enable_one_shot_harvest",
            "enable_plan_control",
            "show_harvested_in_map",
            "dispatch_concurrency",
            # Retired: config held env *names*; now holds model/thinking values.
            "llm_model_env",
            "planner_model_env",
            "subagent_model_env",
            # Retired with the multi-step navigate loop.
            "navigate_max_steps",
            "enable_depth0_oversize_to_dispatch",
            "depth0_oversize_char_limit",
            "compose_group_rank_max_chars",
        ):
            flat.pop(dead, None)
        flat["mode"] = "checklist"
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        cfg = cls(**{k: v for k, v in flat.items() if k in allowed})
        if cfg.map_mode and cfg.llm_max_tokens < 256:
            cfg.llm_max_tokens = 256
        return cfg


@dataclass
class SectionView:
    section_id: str
    level: int
    preview: str
    score: float = 0.0
    n_lines: int = 0
    n_chunks: int = 0
    has_children: bool = False
    depth_from_scope: int = 0
    map_id: str = ""
    title: str = ""
    n_descendants: int = 0
    is_highlight: bool = False
    parent_id: Optional[str] = None
    summary: str = ""
    # PLAN×NAV: subgoal id that collected this node's branch, when the
    # node stays visible (collapsed, no descendant expansion) instead of being
    # deleted from the map — for [harvested:sN].
    harvested_by: str = ""


@dataclass
class Projection:
    doc_id: str
    scope: Optional[str]
    text: str
    visible_sections: List[SectionView]
    truncated: bool = False  # True if any budget-hidden nodes
    id_to_section: Dict[str, str] = field(default_factory=dict)
    map_mode: bool = False
    tree_sections: List[SectionView] = field(default_factory=list)
    highlight_ids: List[str] = field(default_factory=list)


@dataclass
class LegalAction:
    action_id: str
    kind: ActionKind
    section_id: Optional[str] = None
    query: str = ""
    label: str = ""
    score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def prompt_line(self) -> str:
        target = self.section_id or ""
        bits = [self.action_id, self.kind.value.upper()]
        if target:
            bits.append(target)
        if self.label:
            bits.append(self.label)
        if self.score:
            bits.append(f"score={self.score:.4f}")
        return " | ".join(bits)


@dataclass
class SubgoalResult:
    """Typed outcome of one subgoal execution (M5)."""

    subgoal_id: str
    satisfied: bool
    confidence: float = 0.0
    collected_section_ids: List[str] = field(default_factory=list)
    # Explicit COLLECT targets this wave only (hydration descendants omitted).
    explicit_collect_ids: List[str] = field(default_factory=list)
    extracted: Dict[str, str] = field(default_factory=dict)
    gap: str = ""
    chars_used: int = 0


@dataclass
class NavState:
    doc_id: str
    query: str
    task_type: str = "unknown"
    # Working scope for the current harvest level (set by harvest(), not a stack).
    current_scope: Optional[str] = None
    collected_ids: set[str] = field(default_factory=set)
    collected: List[Tuple[Any, float]] = field(default_factory=list)
    map_scores: Dict[str, float] = field(default_factory=dict)
    unit_scores: Dict[str, float] = field(default_factory=dict)
    highlight_ids: List[str] = field(default_factory=list)
    # "Branch done / removed from map" = COLLECT'd sid ∪ all descendants.
    collected_section_ids: set[str] = field(default_factory=set)
    blocked_collect_section_ids: set[str] = field(default_factory=set)
    action_history: List[Dict[str, Any]] = field(default_factory=list)
    refusal_events: List[Dict[str, Any]] = field(default_factory=list)
    dismissed_section_ids: set[str] = field(default_factory=set)
    # Explicit COLLECT confidence by section_id; hydration-only descendants stay 0.
    collect_confidence: Dict[str, float] = field(default_factory=dict)
    # Explicit COLLECT targets only (batch action sids); hydration descendants omitted.
    # Used by plan_control digest and compose progressive trim (protect explicit owners).
    explicit_collect_ids: set[str] = field(default_factory=set)
    # External agent relative priority over nearest-parent groups: parent_id -> score
    # (higher packs first). Empty = no external rerank yet.
    group_priority: Dict[str, float] = field(default_factory=dict)
    # M2: structured retrieval plan + slot bindings for delayed query fill.
    retrieval_plan: Optional[Any] = None
    slot_bindings: Dict[str, str] = field(default_factory=dict)
    satisfied_subgoal_ids: set[str] = field(default_factory=set)
    # Finished trying (accept or drop). Widen leaves a subgoal out of this set.
    attempted_subgoal_ids: set[str] = field(default_factory=set)
    # Soft focus for policy (never clips action space).
    focus_subgoal_id: str = ""
    focus_subgoal_need: str = ""
    focus_subgoal_contract: str = ""
    focus_retrieval_query: str = ""
    focus_contract_kind: str = ""
    subgoal_results: Dict[str, Any] = field(default_factory=dict)
    replan_count: int = 0
    # Checklist: explicit collect-root section_id -> owning subgoal_id
    # (drives "[harvested:sN]" map tags).
    harvested_owner_subgoal: Dict[str, str] = field(default_factory=dict)
    # Last widen gap note per subgoal (what plan_control said was missing).
    # Input to the PLAN-side query rewrite; never concatenated into a query.
    subgoal_widen_gaps: Dict[str, str] = field(default_factory=dict)
    # PLAN's rewritten retrieval_query per subgoal after widen. Overrides the
    # planned query for the next harvest, and re-scores the shared map with it.
    subgoal_refined_queries: Dict[str, str] = field(default_factory=dict)
    # Episode-local map scores keyed by retrieval query. Checklist waves may
    # revisit the same subgoal query; reuse the exact score snapshot instead of
    # rebuilding the persisted index and rescoring the corpus.
    relit_map_cache: Dict[
        str, Tuple[Dict[str, float], Dict[str, float], List[str]]
    ] = field(default_factory=dict)
    # Per-subgoal "seen but not selected" section ids — hidden from later map
    # views for that subgoal so widen surfaces siblings instead of dead ends.
    subgoal_dismissed_section_ids: Dict[str, set[str]] = field(default_factory=dict)
    # Per-subgoal wave-attempt counter (circuit breaker under plan_control).
    subgoal_attempt_counts: Dict[str, int] = field(default_factory=dict)
    # Terminal "drop" outcomes: disjoint from satisfied_subgoal_ids. Union of
    # the two is "settled" for dependency readiness.
    dropped_subgoal_ids: set[str] = field(default_factory=set)
    # SEARCH_* inspector results injected into the next harvest observation
    # (Knowhere asset tool context). Cleared/overwritten by apply_search_assets.
    asset_observation_context: str = ""
