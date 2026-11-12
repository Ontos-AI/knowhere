"""Walk from the current greenlet back to the greenlet that spawned it.

Greenlets started from a ``gevent.pool.Pool`` (or ``gevent.spawn``) run with
the hub as their ``parent``, so the parent chain never reaches the parse-task
greenlet that registered per-task state. gevent records the spawning greenlet
(``spawning_greenlet``, on by default via ``track_greenlet_tree``); following it
first keeps per-task registries (token usage, LLM overrides, stage timings)
visible in pooled work, including nested pools.
"""

from __future__ import annotations

from collections.abc import Iterator


def greenlet_lineage() -> Iterator[object]:
    """Yield the current greenlet, then its spawners, then hub-level parents."""
    try:
        import gevent
    except ImportError:
        # gevent is worker-only; without it there is no greenlet tree to walk.
        return
    current: object | None = gevent.getcurrent()
    while current is not None:
        yield current
        spawner_ref = getattr(current, "spawning_greenlet", None)
        spawner = spawner_ref() if spawner_ref is not None else None
        current = spawner if spawner is not None else getattr(current, "parent", None)
