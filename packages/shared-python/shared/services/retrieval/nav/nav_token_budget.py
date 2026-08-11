"""Episode LLM token hard stop.

One counter, one rule: if cumulative LLM ``total_tokens`` reaches the episode
``token_limit`` (from ``NavConfig``, else ``RETRIEVAL_NAV_TOKEN_LIMIT`` / default),
do not start another nav LLM call; keep already-collected evidence and return
upward (FINISH / done / fallback).

Inside ``nav_token_episode()`` the counter is per-episode (contextvars).
Outside it, usage falls back to the process-global ``llm_usage`` snapshot
so bin scripts that only ``reset_usage()`` keep working.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, List, Optional

_ENV_TOKEN_LIMIT = "RETRIEVAL_NAV_TOKEN_LIMIT"
_DEFAULT_TOKEN_LIMIT = 100_000

# Mutable one-element list so callers can increment without rebinding ContextVar.
_EpisodeCounter = List[int]
_episode_tokens: ContextVar[Optional[_EpisodeCounter]] = ContextVar(
    "nav_episode_tokens", default=None
)
_episode_limit: ContextVar[Optional[int]] = ContextVar(
    "nav_episode_token_limit", default=None
)
_last_stamp_tokens: ContextVar[Optional[int]] = ContextVar(
    "nav_last_stamp_tokens", default=None
)


class NavTokenLimit(Exception):
    """Raised when the episode (or process) LLM token budget is exhausted."""

    def __init__(self, used: int = 0, limit: int = 0) -> None:
        self.used = int(used)
        self.limit = int(limit)
        super().__init__(
            f"nav token limit exhausted: used={self.used} limit={self.limit}"
        )


def nav_token_limit() -> int:
    """Always a positive limit: episode bind → env → default."""
    bound = _episode_limit.get()
    if bound is not None and int(bound) > 0:
        return int(bound)
    try:
        limit = int(os.environ.get(_ENV_TOKEN_LIMIT, "").strip())
    except ValueError:
        limit = 0
    return limit if limit > 0 else _DEFAULT_TOKEN_LIMIT


def _process_tokens_used() -> int:
    from ._compat import snapshot_usage  # type: ignore

    total = 0
    for block in snapshot_usage().values():
        total += int(block.get("total_tokens", 0) or 0)
    return total


def nav_tokens_used() -> int:
    ep = _episode_tokens.get()
    if ep is not None:
        return int(ep[0])
    return _process_tokens_used()


def nav_token_budget_exhausted() -> bool:
    return nav_tokens_used() >= nav_token_limit()


def record_episode_tokens(usage: Optional[Dict[str, Any]]) -> None:
    """Add one call's ``total_tokens`` to the active episode counter (no-op outside)."""
    ep = _episode_tokens.get()
    if ep is None:
        return
    try:
        add = int((usage or {}).get("total_tokens", 0) or 0)
    except (TypeError, ValueError):
        add = 0
    if add > 0:
        ep[0] += add


def stamp_step_detail(
    detail: Optional[Dict[str, Any]] = None,
    *,
    t0: Optional[float] = None,
) -> Dict[str, Any]:
    """Annotate an AgentStep detail with episode token counters and step elapsed_ms."""
    stamped = dict(detail or {})
    used = int(nav_tokens_used())
    prev = _last_stamp_tokens.get()
    if prev is None:
        prev = 0
    stamped["token_limit"] = int(nav_token_limit())
    stamped["tokens_used_total"] = used
    stamped["tokens_used_delta"] = max(0, used - int(prev))
    if t0 is not None:
        stamped["elapsed_ms"] = int(round((time.perf_counter() - float(t0)) * 1000.0))
    _last_stamp_tokens.set(used)
    return stamped


@contextmanager
def nav_token_episode(*, token_limit: int = 0) -> Iterator[None]:
    """Bind a fresh per-episode token counter (and optional hard limit)."""
    token = _episode_tokens.set([0])
    stamp_token = _last_stamp_tokens.set(0)
    limit_arg = int(token_limit or 0)
    limit_token = _episode_limit.set(limit_arg if limit_arg > 0 else None)
    try:
        yield
    finally:
        _episode_limit.reset(limit_token)
        _last_stamp_tokens.reset(stamp_token)
        _episode_tokens.reset(token)
