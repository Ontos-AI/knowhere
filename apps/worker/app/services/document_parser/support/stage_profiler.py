"""Structured stage timing helper for the document parsing pipeline.

Stage-timing accumulation uses the same greenlet-parent-chain approach
as token_tracking.py — see that module's docstring for rationale.
"""

from __future__ import annotations

from contextlib import contextmanager
from time import perf_counter
from typing import Any, Iterator
import threading

from loguru import logger


# ── Greenlet-safe stage timing accumulator ──

_trackers: dict[int, dict[str, int]] = {}
_lock = threading.Lock()
_root_ids: dict[int, int] = {}


def _current_greenlet_id() -> int:
    try:
        import gevent
        return id(gevent.getcurrent())
    except ImportError:
        import threading as _threading
        return _threading.get_ident()


def _find_root_id() -> int | None:
    """Walk up the greenlet parent chain to find a registered root id."""
    gid = _current_greenlet_id()
    if gid in _trackers:
        return gid
    if gid in _root_ids:
        return _root_ids[gid]
    try:
        import gevent
        g = gevent.getcurrent()
        while g is not None:
            pid = id(g)
            if pid in _trackers:
                _root_ids[gid] = pid
                return pid
            g = getattr(g, 'parent', None)
    except ImportError:
        pass
    return None


def init_stage_tracker() -> dict[str, int]:
    """Create a new stage timing accumulator for the current parse task."""
    gid = _current_greenlet_id()
    tracker: dict[str, int] = {}
    with _lock:
        _trackers[gid] = tracker
    return tracker


def cleanup_stage_tracker() -> None:
    """Remove the stage tracker for the current greenlet."""
    gid = _current_greenlet_id()
    with _lock:
        _trackers.pop(gid, None)
        stale = [k for k, v in _root_ids.items() if v == gid]
        for k in stale:
            del _root_ids[k]


# ── Public timer context manager (unchanged API) ──

def _compact_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values so timing logs stay compact and readable."""
    compacted_fields: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        compacted_fields[key] = value
    return compacted_fields


@contextmanager
def stage_timer(stage: str, **fields: Any) -> Iterator[None]:
    """Log elapsed time for a parsing stage without changing control flow."""
    start_time: float = perf_counter()
    compact_fields: dict[str, Any] = _compact_fields(fields)

    try:
        yield
    except Exception:
        elapsed_ms: int = int((perf_counter() - start_time) * 1000)
        logger.bind(
            event="document_parser.stage",
            stage=stage,
            elapsed_ms=elapsed_ms,
            status="error",
            **compact_fields,
        ).warning(f"Stage failed: {stage}")
        raise

    elapsed_ms = int((perf_counter() - start_time) * 1000)

    root = _find_root_id()
    if root is not None:
        tracker = _trackers.get(root)
        if tracker is not None:
            with _lock:
                tracker[stage] = tracker.get(stage, 0) + elapsed_ms

    logger.bind(
        event="document_parser.stage",
        stage=stage,
        elapsed_ms=elapsed_ms,
        status="ok",
        **compact_fields,
    ).info(f"Stage completed: {stage}")
