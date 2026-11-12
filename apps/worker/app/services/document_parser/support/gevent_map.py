from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

import gevent
from gevent.pool import Pool as GeventPool

from shared.services.ai.llm_overrides import (
    bind_llm_overrides,
    get_current_llm_overrides_root_id,
)
from shared.services.ai.token_tracking import (
    bind_token_tracker,
    get_current_token_tracker_root_id,
)

_Item = TypeVar("_Item")
_Result = TypeVar("_Result")


def gevent_map(
    function: Callable[[_Item], _Result],
    items: Sequence[_Item],
    *,
    max_concurrent: int,
) -> list[_Result]:
    """Run ``function`` over ``items`` in a bounded gevent pool, keeping order.

    Pooled greenlets hang off the gevent hub, not the calling task greenlet, so
    the task's token tracker and LLM overrides are bound into each call
    explicitly. An exception in any call propagates to the caller.
    """
    if not items:
        return []
    tracker_root_id = get_current_token_tracker_root_id()
    overrides_root_id = get_current_llm_overrides_root_id()

    def _bound_call(item: _Item) -> _Result:
        with bind_token_tracker(tracker_root_id), bind_llm_overrides(overrides_root_id):
            return function(item)

    pool = GeventPool(size=max(1, min(max_concurrent, len(items))))
    greenlets = [pool.spawn(_bound_call, item) for item in items]
    gevent.joinall(greenlets, raise_error=True)
    return [greenlet.get() for greenlet in greenlets]
