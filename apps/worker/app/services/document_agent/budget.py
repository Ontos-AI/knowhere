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
    """A minimal counter with the same public shape as retrieval's ledger."""

    def __init__(self, *, plan_budget: int = 5000, max_tool_calls: int = 12) -> None:
        self._plan = BudgetPool(capacity=max(int(plan_budget), 0))
        self._max_tool_calls = max(int(max_tool_calls), 1)
        self._tool_calls = 0

    def increment_tool_call(self) -> bool:
        if self._tool_calls >= self._max_tool_calls:
            return False
        self._tool_calls += 1
        return True

    def try_reserve(self, pool: str, est: int) -> bool:
        if pool != "plan":
            return True
        est = max(int(est), 0)
        if self._plan.remaining < est:
            return False
        self._plan.reserved += est
        return True

    def commit(self, pool: str, *, actual: int, est: int) -> None:
        if pool != "plan":
            return
        est = max(int(est), 0)
        actual = max(int(actual), 0)
        self._plan.reserved = max(self._plan.reserved - est, 0)
        self._plan.used = min(self._plan.capacity, self._plan.used + actual)

    def refund(self, pool: str, *, est: int) -> None:
        if pool != "plan":
            return
        self._plan.reserved = max(self._plan.reserved - max(int(est), 0), 0)

    def snapshot(self) -> dict[str, object]:
        return {
            "plan": {
                "capacity": self._plan.capacity,
                "used": self._plan.used,
                "reserved": self._plan.reserved,
                "remaining": self._plan.remaining,
            },
            "tool_calls": self._tool_calls,
            "max_tool_calls": self._max_tool_calls,
        }
