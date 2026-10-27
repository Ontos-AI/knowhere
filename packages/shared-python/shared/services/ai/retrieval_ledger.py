"""Request-scoped retrieval LLM ledger, separate from the parse token tracker.

Opened only on the retrieval query path. Successful provider calls append
one detail row with tokens and duration. Inactive ledger is a no-op.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetrievalCallRecord:
    task: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    duration_ms: int


@dataclass
class RetrievalLedger:
    started_at: float = field(default_factory=time.monotonic)
    entries: list[RetrievalCallRecord] = field(default_factory=list)

    def wall_ms(self) -> int:
        return max(int((time.monotonic() - self.started_at) * 1000), 0)

    def total_tokens_or_none(self) -> int | None:
        if not self.entries:
            return None
        totals = [entry.total_tokens for entry in self.entries]
        if any(total is None for total in totals):
            return None
        return sum(int(total or 0) for total in totals)


_ledger: ContextVar[RetrievalLedger | None] = ContextVar(
    "retrieval_llm_ledger",
    default=None,
)


def init_retrieval_ledger() -> RetrievalLedger:
    ledger = RetrievalLedger()
    _ledger.set(ledger)
    return ledger


def get_current_retrieval_ledger() -> RetrievalLedger | None:
    return _ledger.get()


def cleanup_retrieval_ledger() -> None:
    _ledger.set(None)


def record_retrieval_call(
    usage: dict[str, Any] | None,
    *,
    model: str | None = None,
    task: str | None = None,
    duration_ms: int = 0,
) -> None:
    """Append one model call when a retrieval ledger is active."""
    ledger = _ledger.get()
    if ledger is None:
        return
    task_key = str(task or "").strip() or "unknown"
    model_key = str(model or "").strip() or "unknown"
    ledger.entries.append(
        RetrievalCallRecord(
            task=task_key,
            model=model_key,
            prompt_tokens=_optional_token(usage, "prompt_tokens"),
            completion_tokens=_optional_token(usage, "completion_tokens"),
            total_tokens=_optional_token(usage, "total_tokens"),
            duration_ms=max(int(duration_ms), 0),
        )
    )


def _optional_token(usage: dict[str, Any] | None, key: str) -> int | None:
    if usage is None:
        return None
    if key not in usage or usage.get(key) is None:
        return None
    try:
        return int(usage.get(key) or 0)
    except (TypeError, ValueError):
        return None
