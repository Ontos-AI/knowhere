"""Top-level wallet for decomposed retrieval workflows."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

from shared.services.retrieval.agentic.core.budget import BudgetLedger
from shared.services.retrieval.workflow.types import PlannedStep, QueryPlan


_RETRIEVE_FLOOR = 4000
_SYNTHESIZE_FLOOR = 1500


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class BudgetWallet:
    """Issue per-step ``BudgetLedger`` instances under a workflow hard cap."""

    total: int
    per_retrieve_step_default: int
    per_synthesize_step_default: int
    # Read from env for consistency with _build_config_from_env()
    planning_ratio: float = field(
        default_factory=lambda: _env_float('RETRIEVAL_AGENTIC_PLANNING_RATIO', 0.5)
    )
    bootstrap_budget: int = field(
        default_factory=lambda: _env_int('RETRIEVAL_AGENTIC_BOOTSTRAP_BUDGET', 2000)
    )
    per_doc_min_share: int = field(
        default_factory=lambda: _env_int('RETRIEVAL_AGENTIC_PER_DOC_MIN_SHARE', 1500)
    )
    _allocations: dict[str, int] = field(default_factory=dict, init=False)
    _reclaimed: dict[str, int] = field(default_factory=dict, init=False)
    _ledgers: dict[str, BudgetLedger] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def allocate(self, plan: QueryPlan) -> dict[str, BudgetLedger]:
        """Pre-allocate one ledger per step, scaling down when needed."""
        async with self._lock:
            requested = {
                step.id: self._requested_for_step(step)
                for step in plan.steps
            }
            requested_total = sum(requested.values())
            if requested_total <= 0:
                requested_total = 1

            if requested_total <= self.total:
                allocations = requested
            else:
                scale = max(self.total, 1) / requested_total
                allocations = {}
                for step in plan.steps:
                    floor = _RETRIEVE_FLOOR if step.step_kind == "retrieve" else _SYNTHESIZE_FLOOR
                    allocations[step.id] = max(floor, int(requested[step.id] * scale))

                scaled_total = sum(allocations.values())
                if scaled_total > self.total:
                    # Respect the hard cap by reducing largest allocations first,
                    # never dropping below the per-kind floors.
                    excess = scaled_total - self.total
                    for step_id, amount in sorted(
                        allocations.items(), key=lambda item: item[1], reverse=True
                    ):
                        if excess <= 0:
                            break
                        step = next(s for s in plan.steps if s.id == step_id)
                        floor = _RETRIEVE_FLOOR if step.step_kind == "retrieve" else _SYNTHESIZE_FLOOR
                        reducible = max(amount - floor, 0)
                        delta = min(reducible, excess)
                        allocations[step_id] = amount - delta
                        excess -= delta

            self._allocations = allocations
            self._ledgers = {
                step.id: self._new_ledger(step, allocations[step.id])
                for step in plan.steps
            }
            return dict(self._ledgers)

    async def reclaim(self, step_id: str, ledger: BudgetLedger) -> None:
        """Record unused capacity after a step completes."""
        async with self._lock:
            allocated = self._allocations.get(step_id, 0)
            used = self._ledger_used(ledger)
            self._reclaimed[step_id] = max(allocated - used, 0)

    def total_used(self) -> int:
        return sum(self._ledger_used(ledger) for ledger in self._ledgers.values())

    def snapshot(self) -> dict[str, object]:
        return {
            "total": self.total,
            "allocated": sum(self._allocations.values()),
            "used": self.total_used(),
            "remaining": max(self.total - self.total_used(), 0),
            "allocations": dict(self._allocations),
            "reclaimed": dict(self._reclaimed),
            "steps": {
                step_id: ledger.snapshot()
                for step_id, ledger in self._ledgers.items()
            },
        }

    def _requested_for_step(self, step: PlannedStep) -> int:
        if step.step_kind == "synthesize":
            return max(self.per_synthesize_step_default, _SYNTHESIZE_FLOOR)
        return max(self.per_retrieve_step_default, _RETRIEVE_FLOOR)

    def _new_ledger(self, step: PlannedStep, total: int) -> BudgetLedger:
        if step.step_kind == "synthesize":
            # Put almost all tokens into context for pure synthesis calls.
            return BudgetLedger(
                total=max(total, 1),
                planning_ratio=0.0,
                bootstrap=0,
                per_doc_min_share=0,
            )
        return BudgetLedger(
            total=max(total, 1),
            planning_ratio=self.planning_ratio,
            bootstrap=min(self.bootstrap_budget, max(total, 1)),
            per_doc_min_share=self.per_doc_min_share,
        )

    @staticmethod
    def _ledger_used(ledger: BudgetLedger) -> int:
        snapshot = ledger.snapshot()
        total = 0
        for pool in ("bootstrap", "planning", "context"):
            pool_state = snapshot.get(pool) or {}
            if isinstance(pool_state, dict):
                total += int(pool_state.get("used") or 0)
                total += int(pool_state.get("reserved") or 0)
        return total
