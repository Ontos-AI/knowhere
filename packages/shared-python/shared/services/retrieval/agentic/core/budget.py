"""Token budget ledger for agentic retrieval runs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal


BudgetPoolName = Literal["bootstrap", "planning", "context"]
BudgetStatus = Literal["HEALTHY", "TIGHT", "CRITICAL", "EXHAUSTED"]


class BudgetExceeded(Exception):
    """Raised when a planned LLM call cannot reserve budget."""


@dataclass
class BudgetPool:
    name: BudgetPoolName
    capacity: int
    used: int = 0
    reserved: int = 0

    @property
    def remaining(self) -> int:
        return max(self.capacity - self.used - self.reserved, 0)

    @property
    def used_pct(self) -> int:
        if self.capacity <= 0:
            return 100
        return min(100, int(round((self.used + self.reserved) * 100 / self.capacity)))


class BudgetLedger:
    """Concurrency-safe ledger with bootstrap/planning/context pools."""

    def __init__(
        self,
        *,
        total: int,
        planning_ratio: float,
        bootstrap: int = 2000,
        per_doc_min_share: int = 1500,
    ) -> None:
        total = max(int(total), 1)
        bootstrap = max(0, min(int(bootstrap), total))
        remaining = max(total - bootstrap, 0)
        planning_ratio = min(max(float(planning_ratio), 0.0), 1.0)
        planning_capacity = int(remaining * planning_ratio)
        context_capacity = remaining - planning_capacity

        self._lock = asyncio.Lock()
        self._pools: dict[BudgetPoolName, BudgetPool] = {
            "bootstrap": BudgetPool("bootstrap", bootstrap),
            "planning": BudgetPool("planning", planning_capacity),
            "context": BudgetPool("context", context_capacity),
        }
        self._doc_caps: dict[str, int] = {}
        self._doc_used: dict[str, int] = {}
        self._doc_reserved: dict[str, int] = {}
        self._per_doc_min_share = max(int(per_doc_min_share), 0)
        self.total_chunks = 0
        self.total_docs = 0
        self.explored_chunks = 0
        self.explored_docs = 0
        self.trimmed_paths: list[dict[str, Any]] = []

    def remaining(self, pool: BudgetPoolName) -> int:
        return self._pools[pool].remaining

    def status(self, pool: BudgetPoolName) -> BudgetStatus:
        pool_state = self._pools[pool]
        if pool_state.remaining <= 0:
            return "EXHAUSTED"
        used_pct = pool_state.used_pct
        if used_pct >= 80:
            return "CRITICAL"
        if used_pct >= 50:
            return "TIGHT"
        return "HEALTHY"

    async def allocate_doc_caps(self, doc_chunks: dict[str, int]) -> None:
        """Allocate planning soft caps by document chunk counts."""
        async with self._lock:
            self._doc_caps.clear()
            self._doc_used.clear()
            self._doc_reserved.clear()
            if not doc_chunks:
                return

            planning_capacity = self._pools["planning"].capacity
            total_weight = sum(max(int(count), 1) for count in doc_chunks.values())
            for doc_id, count in doc_chunks.items():
                weight = max(int(count), 1)
                weighted = int(planning_capacity * weight / total_weight)
                self._doc_caps[doc_id] = min(
                    planning_capacity,
                    max(self._per_doc_min_share, weighted),
                )

    async def try_reserve(
        self,
        pool: BudgetPoolName,
        est: int,
        doc_id: str | None = None,
        *,
        priority: Literal["normal", "low"] = "normal",
    ) -> bool:
        est = max(int(est), 0)
        if est == 0:
            return True

        async with self._lock:
            pool_state = self._pools[pool]
            if priority == "low" and self.status(pool) == "CRITICAL":
                return False
            if pool_state.remaining < est:
                return False

            pool_state.reserved += est
            if pool == "planning" and doc_id:
                self._doc_reserved[doc_id] = self._doc_reserved.get(doc_id, 0) + est
            return True

    async def commit(
        self,
        pool: BudgetPoolName,
        *,
        actual: int,
        est: int,
        doc_id: str | None = None,
    ) -> None:
        actual = max(int(actual), 0)
        est = max(int(est), 0)
        async with self._lock:
            pool_state = self._pools[pool]
            reserved_delta = min(est, pool_state.reserved)
            pool_state.reserved -= reserved_delta
            pool_state.used = min(pool_state.capacity, pool_state.used + actual)

            if pool == "planning" and doc_id:
                doc_reserved = min(est, self._doc_reserved.get(doc_id, 0))
                if doc_reserved:
                    self._doc_reserved[doc_id] -= doc_reserved
                    if self._doc_reserved[doc_id] <= 0:
                        self._doc_reserved.pop(doc_id, None)
                self._doc_used[doc_id] = self._doc_used.get(doc_id, 0) + actual

    async def refund(
        self,
        pool: BudgetPoolName,
        *,
        est: int,
        doc_id: str | None = None,
    ) -> None:
        est = max(int(est), 0)
        async with self._lock:
            pool_state = self._pools[pool]
            pool_state.reserved = max(pool_state.reserved - est, 0)
            if pool == "planning" and doc_id:
                current = self._doc_reserved.get(doc_id, 0)
                remaining = max(current - est, 0)
                if remaining:
                    self._doc_reserved[doc_id] = remaining
                else:
                    self._doc_reserved.pop(doc_id, None)

    def mark_explored(
        self,
        *,
        chunks: int = 0,
        docs: int = 0,
    ) -> None:
        self.explored_chunks += max(int(chunks), 0)
        self.explored_docs += max(int(docs), 0)

    def snapshot(self) -> dict[str, object]:
        snapshot: dict[str, object] = {
            name: {
                "capacity": pool.capacity,
                "used": pool.used,
                "reserved": pool.reserved,
                "remaining": pool.remaining,
                "used_pct": pool.used_pct,
                "status": self.status(name),
            }
            for name, pool in self._pools.items()
        }
        snapshot.update({
            "total_chunks": self.total_chunks,
            "total_docs": self.total_docs,
            "explored_chunks": min(self.explored_chunks, self.total_chunks)
            if self.total_chunks else self.explored_chunks,
            "explored_docs": min(self.explored_docs, self.total_docs)
            if self.total_docs else self.explored_docs,
            "trimmed_paths": list(self.trimmed_paths),
        })
        return snapshot
