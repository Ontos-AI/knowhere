"""Runtime configuration for decomposed retrieval workflows."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowRuntimeConfig:
    planner_budget: int = 4000
    wallet_total_budget: int = 200000
    per_retrieve_step_budget: int = 40000
    per_synthesize_step_budget: int = 6000
    max_steps: int = 5
    parallel_max: int = 3

    @classmethod
    def from_env(cls) -> "WorkflowRuntimeConfig":
        return cls(
            planner_budget=_env_int("RETRIEVAL_PLANNER_THINKING_BUDGET", 4000),
            wallet_total_budget=_env_int("RETRIEVAL_WALLET_TOTAL_BUDGET", 200000),
            per_retrieve_step_budget=_env_int("RETRIEVAL_WALLET_PER_RETRIEVE_STEP_BUDGET", 40000),
            per_synthesize_step_budget=_env_int("RETRIEVAL_WALLET_PER_SYNTHESIZE_STEP_BUDGET", 6000),
            max_steps=_env_int("RETRIEVAL_DECOMPOSITION_MAX_STEPS", 5),
            parallel_max=_env_int("RETRIEVAL_WORKFLOW_PARALLEL_MAX", 3),
        )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
