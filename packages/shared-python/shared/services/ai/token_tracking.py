"""Greenlet-safe token usage tracker for document parsing pipeline.

In the gevent worker, child greenlets (GeventPool.spawn) do NOT inherit
``ContextVar`` or ``threading.local`` from the parent.  We therefore use
a module-level dict keyed by the *root* greenlet id of the current parse
task.  ``init_tracker`` sets the greenlet id; ``record_tokens`` looks it
up the chain via ``gevent.getcurrent()`` so that child greenlets spawned
within the same parse task all accumulate into the same dict.

Thread-safety: multiple parse tasks in the same worker process each have
distinct root greenlet ids, so their accumulators never collide.
"""

from __future__ import annotations

import threading
from typing import Any

_trackers: dict[int, dict[str, Any]] = {}
_lock = threading.Lock()

# The root greenlet id for the current parse task.  Stored so that child
# greenlets (which cannot inherit ContextVar) can be associated back to
# their root.  We walk the greenlet parent chain to find the id.
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
    # Check self first (fastest path for the root greenlet)
    if gid in _trackers:
        return gid
    # Check if this greenlet was registered as a child
    if gid in _root_ids:
        return _root_ids[gid]
    # Walk parent chain
    try:
        import gevent
        g = gevent.getcurrent()
        while g is not None:
            pid = id(g)
            if pid in _trackers:
                # Cache for future lookups
                _root_ids[gid] = pid
                return pid
            g = getattr(g, 'parent', None)
    except ImportError:
        pass
    return None


def init_token_tracker() -> dict[str, Any]:
    """Create a new token accumulator for the current parse task.

    Must be called from the root greenlet of the task (i.e. from
    ``execute_document_parse``).  Returns the mutable dict that will
    accumulate all token usage for the lifetime of this task.
    """
    gid = _current_greenlet_id()
    tracker: dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
        "by_model": {},
        "by_task": {},
    }
    with _lock:
        _trackers[gid] = tracker
    return tracker


def get_current_token_tracker() -> dict[str, Any] | None:
    """Return the active token accumulator for this task, if any."""
    root = _find_root_id()
    if root is None:
        return None
    return _trackers.get(root)


def cleanup_token_tracker() -> None:
    """Remove the tracker for the current greenlet.  Call after parsing."""
    gid = _current_greenlet_id()
    with _lock:
        _trackers.pop(gid, None)
        # Also clean any child mappings that pointed to this root
        stale = [k for k, v in _root_ids.items() if v == gid]
        for k in stale:
            del _root_ids[k]


def _normalize_bucket_key(value: str | None, fallback: str) -> str:
    normalized = str(value or "").strip()
    return normalized or fallback


def _add_usage(bucket: dict[str, Any], usage: dict[str, int]) -> None:
    bucket["prompt_tokens"] = bucket.get("prompt_tokens", 0) + usage.get("prompt_tokens", 0)
    bucket["completion_tokens"] = bucket.get("completion_tokens", 0) + usage.get("completion_tokens", 0)
    bucket["total_tokens"] = bucket.get("total_tokens", 0) + usage.get("total_tokens", 0)
    bucket["calls"] = bucket.get("calls", 0) + 1


def record_tokens(
    usage: dict[str, int],
    *,
    model: str | None = None,
    task: str | None = None,
) -> None:
    """Accumulate token usage into the current task's tracker.

    Safe to call from any greenlet (root or child).  If no tracker is
    active (e.g. called from retrieval or outside a parse task), this
    is a silent no-op.
    """
    root = _find_root_id()
    if root is None:
        return
    tracker = _trackers.get(root)
    if tracker is None:
        return
    model_key = _normalize_bucket_key(model, "unknown")
    task_key = _normalize_bucket_key(task, "unknown")
    with _lock:
        _add_usage(tracker, usage)
        by_model = tracker.setdefault("by_model", {})
        if isinstance(by_model, dict):
            model_bucket = by_model.setdefault(model_key, {})
            if isinstance(model_bucket, dict):
                _add_usage(model_bucket, usage)
        by_task = tracker.setdefault("by_task", {})
        if isinstance(by_task, dict):
            task_bucket = by_task.setdefault(task_key, {})
            if isinstance(task_bucket, dict):
                _add_usage(task_bucket, usage)
                task_models = task_bucket.setdefault("models", {})
                if isinstance(task_models, dict):
                    task_model_bucket = task_models.setdefault(model_key, {})
                    if isinstance(task_model_bucket, dict):
                        _add_usage(task_model_bucket, usage)
