"""Small synchronous budget tracker for parse-side agent planning."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BudgetPool:
    capacity: int
    used: int = 0
    reserved: int = 0

    @property
    def remaining(self) -> int:
        return max(self.capacity - self.used - self.reserved, 0)


class BudgetTracker:
    """A minimal synchronous ledger with plan and visual pools."""

    def __init__(
        self,
        *,
        plan_budget: int = 5000,
        visual_budget: int = 8000,
    ) -> None:
        self._plan = BudgetPool(capacity=max(int(plan_budget), 0))
        self._visual = BudgetPool(capacity=max(int(visual_budget), 0))

    def try_reserve(self, pool: str, est: int) -> bool:
        if pool not in {"plan", "visual"}:
            return True
        est = max(int(est), 0)
        budget_pool = self._pool(pool)
        if budget_pool.remaining < est:
            return False
        budget_pool.reserved += est
        return True

    def commit(self, pool: str, *, actual: int, est: int) -> None:
        if pool not in {"plan", "visual"}:
            return
        est = max(int(est), 0)
        actual = max(int(actual), 0)
        budget_pool = self._pool(pool)
        budget_pool.reserved = max(budget_pool.reserved - est, 0)
        budget_pool.used = min(budget_pool.capacity, budget_pool.used + actual)

    def refund(self, pool: str, *, est: int) -> None:
        if pool not in {"plan", "visual"}:
            return
        budget_pool = self._pool(pool)
        budget_pool.reserved = max(budget_pool.reserved - max(int(est), 0), 0)

    def _pool(self, pool: str) -> BudgetPool:
        return self._visual if pool == "visual" else self._plan

    def _pool_snapshot(self, pool: BudgetPool) -> dict[str, int]:
        return {
            "capacity": pool.capacity,
            "used": pool.used,
            "reserved": pool.reserved,
            "remaining": pool.remaining,
        }

    def snapshot(self) -> dict[str, object]:
        return {
            "plan": self._pool_snapshot(self._plan),
            "visual": self._pool_snapshot(self._visual),
        }
