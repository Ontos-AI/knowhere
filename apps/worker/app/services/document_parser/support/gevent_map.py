from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

import gevent
from gevent.pool import Pool as GeventPool

_Item = TypeVar("_Item")
_Result = TypeVar("_Result")


def gevent_map(
    function: Callable[[_Item], _Result],
    items: Sequence[_Item],
    *,
    max_concurrent: int,
) -> list[_Result]:
    """Run ``function`` over ``items`` in a bounded gevent pool, keeping order.

    Per-task state (token usage, LLM overrides, stage timings) reaches pooled
    calls through the greenlet spawn lineage. An exception in any call
    propagates to the caller.
    """
    if not items:
        return []
    pool = GeventPool(size=max(1, min(max_concurrent, len(items))))
    greenlets = [pool.spawn(function, item) for item in items]
    gevent.joinall(greenlets, raise_error=True)
    return [greenlet.get() for greenlet in greenlets]
