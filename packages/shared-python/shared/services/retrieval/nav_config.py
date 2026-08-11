"""Production map-nav config — single source of truth at the retrieval entry.

Values live on ``NavConfig`` (model / thinking / token_limit / evidence chars).
Vendored nav binds them for the episode via ``nav_llm_runtime`` +
``nav_token_episode``; no process-wide env seeding and no Knowhere wallet/agentic knobs.
"""

from __future__ import annotations

from typing import Any

from shared.services.retrieval.nav.nav_types import NavConfig

# Migrated probe / llm_api.env stack.
MAPNAV_MODEL = "deepseek-v4-flash"
MAPNAV_EVIDENCE_CHARS = 12_000
MAPNAV_TOKEN_LIMIT = 100_000
MAPNAV_PLANNER_THINK_MAX_TOKENS = 16_384
MAPNAV_TRACE_RAW_CHARS = 2_000

# Hard-coded enabled product config (checklist + map). Matches EXP
# config/nav_default.json + cfg_shared() (mode=checklist, map_mode=True).
_PRODUCTION_NAV_DICT: dict[str, Any] = {
    "projection_depth": 2,
    "projection_child_limit": 8,
    "projection_char_limit": 8000,
    "summary_chars": 120,
    "max_steps": 8,
    "collect_k": 64,
    "search_k": 40,
    "collect_top_k": 6,
    "read_score_bonus": 10.0,
    "policy": "llm",
    "llm_model": MAPNAV_MODEL,
    "llm_temperature": 0.0,
    "llm_max_tokens": 256,
    "planner_llm_max_tokens": 1024,
    "harvest_llm_max_tokens": 1024,
    "budget_modes": {
        "critical_remaining_steps": 1,
        "tight_remaining_steps": 2,
    },
    "map_mode": True,
    "map_char_limit": 5000,
    "map_children_limit": 10000,
    "enable_recursive_dispatch": True,
    "max_dispatch_depth": 3,
    "navigate_max_steps": 8,
    "subagent_model": MAPNAV_MODEL,
    "scope_inline_summary_char_limit": 1500,
    "scope_inline_summary_budget_mult": 3.0,
    "compose_confidence_weight": 0.5,
    "compose_group_rank_max_chars": 10000,
    "enable_depth0_oversize_to_dispatch": True,
    "depth0_oversize_char_limit": 500,
    "mode": "checklist",
    "planning_map_char_limit": 10000,
    "planner_max_subgoals": 0,
    "planner_model": MAPNAV_MODEL,
    "planner_thinking": "enabled",
    "planner_think_max_tokens": MAPNAV_PLANNER_THINK_MAX_TOKENS,
    "token_limit": MAPNAV_TOKEN_LIMIT,
    "subgoal_max_attempts": 2,
    "max_replans": 1,
    "max_waves": 0,
    "max_harvest_depth": 3,
    "plan_control_digest_chars": 600,
}


def nav_evidence_chars() -> int:
    """Evidence pack budget — code constant."""
    return MAPNAV_EVIDENCE_CHARS


def build_nav_config() -> NavConfig:
    """Checklist + map_mode production config (enabled items only)."""
    cfg = NavConfig.from_dict(dict(_PRODUCTION_NAV_DICT))
    cfg.mode = "checklist"
    cfg.map_mode = True
    cfg.policy = "llm"
    return cfg
