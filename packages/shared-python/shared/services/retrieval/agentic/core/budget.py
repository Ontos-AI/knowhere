"""Token budget ledger for agentic retrieval runs."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from typing import Any, Literal


BudgetPoolName = Literal["bootstrap", "planning", "context"]
BudgetStatus = Literal["HEALTHY", "TIGHT", "CRITICAL", "EXHAUSTED"]


def status_from_usage(*, remaining: int, used_pct: int) -> BudgetStatus:
    if remaining <= 0:
        return "EXHAUSTED"
    if used_pct >= 80:
        return "CRITICAL"
    if used_pct >= 50:
        return "TIGHT"
    return "HEALTHY"


def project_budget_snapshot(
    snapshot: dict | None,
    *,
    pool: BudgetPoolName,
    additional_tokens: int,
) -> dict | None:
    """Return a snapshot projected after an estimated upcoming token cost."""
    if not snapshot:
        return snapshot
    adjusted = copy.deepcopy(snapshot)
    pool_data = adjusted.get(pool)
    if not isinstance(pool_data, dict):
        return adjusted

    capacity = int(pool_data.get("capacity") or 0)
    used = int(pool_data.get("used") or 0)
    reserved = int(pool_data.get("reserved") or 0)
    projected_used_total = max(0, used + reserved + max(int(additional_tokens), 0))
    used_pct = (
        int(round(projected_used_total * 100 / capacity))
        if capacity > 0 else 100
    )
    remaining = max(capacity - projected_used_total, 0)
    pool_data["used_pct"] = used_pct
    pool_data["remaining"] = remaining
    pool_data["status"] = status_from_usage(
        remaining=remaining,
        used_pct=used_pct,
    )
    return adjusted


def budget_status_from_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    pool: BudgetPoolName = "planning",
) -> str:
    """Read a pool status from a serialized budget snapshot."""
    if not isinstance(snapshot, dict):
        return "UNKNOWN"
    pool_data = snapshot.get(pool)
    if not isinstance(pool_data, dict):
        return "UNKNOWN"
    return str(pool_data.get("status") or "UNKNOWN")


class BudgetExceeded(Exception):
    """Raised when a planned LLM call cannot reserve budget."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


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
        return int(round((self.used + self.reserved) * 100 / self.capacity))


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
        self._overdraft_events: list[dict[str, Any]] = []

    def remaining(self, pool: BudgetPoolName) -> int:
        return self._pools[pool].remaining

    def status(self, pool: BudgetPoolName) -> BudgetStatus:
        pool_state = self._pools[pool]
        return status_from_usage(
            remaining=pool_state.remaining,
            used_pct=pool_state.used_pct,
        )

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
        reservation = await self.reserve(
            pool,
            est,
            doc_id=doc_id,
            priority=priority,
            allow_overdraft=False,
        )
        return bool(reservation.get("reserved"))

    async def reserve(
        self,
        pool: BudgetPoolName,
        est: int,
        doc_id: str | None = None,
        *,
        priority: Literal["normal", "low"] = "normal",
        allow_overdraft: bool = False,
        overdraft_reason: str = "",
    ) -> dict[str, Any]:
        est = max(int(est), 0)
        if est == 0:
            return {"reserved": True, "overdraft": False, "failure": None}

        async with self._lock:
            pool_state = self._pools[pool]
            if priority == "low" and self.status(pool) == "CRITICAL":
                failure = self._reserve_failure(pool, est, doc_id, "low_priority_critical")
                return {"reserved": False, "overdraft": False, "failure": failure}

            failure_reason = ""
            if pool_state.remaining < est:
                failure_reason = "pool_remaining_lt_est"

            # Per-doc cap enforcement: prevent one document from consuming
            # the entire planning pool.
            doc_remaining: int | None = None
            if pool == "planning" and doc_id and doc_id in self._doc_caps:
                doc_remaining = self._doc_caps[doc_id] - (
                    self._doc_used.get(doc_id, 0)
                    + self._doc_reserved.get(doc_id, 0)
                )
                if doc_remaining < est:
                    failure_reason = "doc_remaining_lt_est"

            if failure_reason:
                failure = self._reserve_failure(pool, est, doc_id, failure_reason)
                if not allow_overdraft or pool != "planning":
                    return {"reserved": False, "overdraft": False, "failure": failure}
                self._record_overdraft(
                    pool=pool,
                    est=est,
                    doc_id=doc_id,
                    reason=overdraft_reason or failure_reason,
                    failure=failure,
                )

            pool_state.reserved += est
            if pool == "planning" and doc_id:
                self._doc_reserved[doc_id] = self._doc_reserved.get(doc_id, 0) + est
            return {
                "reserved": True,
                "overdraft": bool(failure_reason),
                "failure": self._reserve_failure(pool, est, doc_id, failure_reason)
                if failure_reason else None,
            }

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
            pool_state.used = max(0, pool_state.used + actual)

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
                "overdraft": max(pool.used + pool.reserved - pool.capacity, 0),
                "status": self.status(name),
            }
            for name, pool in self._pools.items()
        }
        if self._overdraft_events:
            snapshot["overdraft_events"] = list(self._overdraft_events)
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

    def _reserve_failure(
        self,
        pool: BudgetPoolName,
        est: int,
        doc_id: str | None,
        reason: str,
    ) -> dict[str, Any]:
        pool_state = self._pools[pool]
        details: dict[str, Any] = {
            "reason": reason,
            "pool": pool,
            "prompt_est": max(int(est), 0),
            "pool_capacity": pool_state.capacity,
            "pool_used": pool_state.used,
            "pool_reserved": pool_state.reserved,
            "pool_remaining": pool_state.remaining,
        }
        if pool == "planning" and doc_id and doc_id in self._doc_caps:
            doc_used = self._doc_used.get(doc_id, 0)
            doc_reserved = self._doc_reserved.get(doc_id, 0)
            details.update({
                "doc_id": doc_id,
                "doc_cap": self._doc_caps[doc_id],
                "doc_used": doc_used,
                "doc_reserved": doc_reserved,
                "doc_remaining": max(self._doc_caps[doc_id] - doc_used - doc_reserved, 0),
            })
        return details

    def _record_overdraft(
        self,
        *,
        pool: BudgetPoolName,
        est: int,
        doc_id: str | None,
        reason: str,
        failure: dict[str, Any],
    ) -> None:
        pool_shortfall = max(int(est) - int(failure.get("pool_remaining") or 0), 0)
        doc_shortfall = 0
        if "doc_remaining" in failure:
            doc_shortfall = max(int(est) - int(failure.get("doc_remaining") or 0), 0)
        self._overdraft_events.append({
            "pool": pool,
            "doc_id": doc_id,
            "prompt_est": max(int(est), 0),
            "shortfall": max(pool_shortfall, doc_shortfall),
            "reason": reason,
            "failure": failure,
        })
