"""Small synchronous budget tracker for parse-side agent planning."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal


BudgetStage = Literal[
    "toc_confirm",
    "coarse_profile",
    "calibration",
    "page_tagging",
]


@dataclass
class BudgetPool:
    capacity: int
    used: int = 0
    reserved: int = 0

    @property
    def remaining(self) -> int:
        return max(self.capacity - self.used - self.reserved, 0)


@dataclass(frozen=True)
class StageEnvelope:
    min_guarantee: int = 0
    cap: int | None = None


@dataclass
class StageUsage:
    used: int = 0
    reserved: int = 0

    @property
    def committed(self) -> int:
        return self.used + self.reserved


class BudgetTracker:
    """A minimal synchronous ledger with plan and visual pools."""

    def __init__(
        self,
        *,
        plan_budget: int = 5000,
        visual_budget: int = 8000,
        visual_stage_envelopes: dict[str, StageEnvelope] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._plan = BudgetPool(capacity=max(int(plan_budget), 0))
        self._visual = BudgetPool(capacity=max(int(visual_budget), 0))
        self._visual_stage_envelopes = visual_stage_envelopes or {}
        self._visual_stage_usage: dict[str, StageUsage] = {
            stage: StageUsage() for stage in self._visual_stage_envelopes
        }

    def try_reserve(self, pool: str, est: int, *, stage: str | None = None) -> bool:
        if pool not in {"plan", "visual"}:
            return True
        est = max(int(est), 0)
        with self._lock:
            budget_pool = self._pool(pool)
            if budget_pool.remaining < est:
                return False
            if pool == "visual" and stage and not self._can_reserve_visual_stage(stage, est):
                return False
            budget_pool.reserved += est
            if pool == "visual" and stage:
                self._stage_usage(stage).reserved += est
            return True

    def commit(
        self,
        pool: str,
        *,
        actual: int,
        est: int,
        stage: str | None = None,
    ) -> None:
        if pool not in {"plan", "visual"}:
            return
        est = max(int(est), 0)
        actual = max(int(actual), 0)
        with self._lock:
            budget_pool = self._pool(pool)
            budget_pool.reserved = max(budget_pool.reserved - est, 0)
            budget_pool.used = min(budget_pool.capacity, budget_pool.used + actual)
            if pool == "visual" and stage:
                stage_usage = self._stage_usage(stage)
                stage_usage.reserved = max(stage_usage.reserved - est, 0)
                stage_usage.used += actual

    def refund(self, pool: str, *, est: int, stage: str | None = None) -> None:
        if pool not in {"plan", "visual"}:
            return
        est = max(int(est), 0)
        with self._lock:
            budget_pool = self._pool(pool)
            budget_pool.reserved = max(budget_pool.reserved - est, 0)
            if pool == "visual" and stage:
                stage_usage = self._stage_usage(stage)
                stage_usage.reserved = max(stage_usage.reserved - est, 0)

    def _pool(self, pool: str) -> BudgetPool:
        return self._visual if pool == "visual" else self._plan

    def _stage_usage(self, stage: str) -> StageUsage:
        if stage not in self._visual_stage_usage:
            self._visual_stage_usage[stage] = StageUsage()
        return self._visual_stage_usage[stage]

    def _can_reserve_visual_stage(self, stage: str, est: int) -> bool:
        envelope = self._visual_stage_envelopes.get(stage)
        if envelope is None:
            return True

        stage_usage = self._stage_usage(stage)
        stage_committed_after_reserve = stage_usage.committed + est
        if envelope.cap is not None and stage_committed_after_reserve > envelope.cap:
            return False

        reserved_by_other_stages = 0
        for other_stage, other_envelope in self._visual_stage_envelopes.items():
            if other_stage == stage:
                continue
            other_usage = self._stage_usage(other_stage)
            if other_usage.committed >= other_envelope.min_guarantee:
                continue
            reserved_by_other_stages += other_envelope.min_guarantee - other_usage.committed

        return self._visual.remaining - est >= reserved_by_other_stages

    def _pool_snapshot(self, pool: BudgetPool) -> dict[str, int]:
        return {
            "capacity": pool.capacity,
            "used": pool.used,
            "reserved": pool.reserved,
            "remaining": pool.remaining,
        }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "plan": self._pool_snapshot(self._plan),
                "visual": self._pool_snapshot(self._visual),
                "visual_stages": {
                    stage: {
                        "used": usage.used,
                        "reserved": usage.reserved,
                        "min_guarantee": self._visual_stage_envelopes.get(
                            stage, StageEnvelope()
                        ).min_guarantee,
                        "cap": self._visual_stage_envelopes.get(
                            stage, StageEnvelope()
                        ).cap,
                    }
                    for stage, usage in sorted(self._visual_stage_usage.items())
                },
            }

    def fork(self, ratio: float) -> "BudgetTracker":
        """Create an independent child tracker with proportional remaining budget."""
        ratio = max(0.0, min(1.0, float(ratio)))
        with self._lock:
            child_envelopes: dict[str, StageEnvelope] = {}
            for stage, envelope in self._visual_stage_envelopes.items():
                usage = self._visual_stage_usage.get(stage, StageUsage())
                remaining_guarantee = max(envelope.min_guarantee - usage.committed, 0)
                child_envelopes[stage] = StageEnvelope(
                    min_guarantee=int(remaining_guarantee * ratio),
                    cap=int(envelope.cap * ratio) if envelope.cap is not None else None,
                )

            return BudgetTracker(
                plan_budget=int(self._plan.remaining * ratio),
                visual_budget=int(self._visual.remaining * ratio),
                visual_stage_envelopes=child_envelopes,
            )
