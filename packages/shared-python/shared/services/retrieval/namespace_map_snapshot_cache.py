"""Process-local cache for decoded namespace MAP snapshot documents.

Keyed by ``(user_id, namespace, generation)`` so a publish/archive that bumps
the namespace generation invalidates stale entries automatically -- no manual
invalidation call is needed. Bounded LRU keeps memory use predictable across
many namespaces sharing one worker process.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

_MAX_ENTRIES = 64
_lock = threading.Lock()
_cache: "OrderedDict[tuple[str, str, int], dict[str, dict[str, Any]]]" = OrderedDict()


def get_cached_namespace_documents(
    *, user_id: str, namespace: str, generation: int
) -> dict[str, dict[str, Any]] | None:
    key = (user_id, namespace, generation)
    with _lock:
        documents = _cache.get(key)
        if documents is not None:
            _cache.move_to_end(key)
        return documents


def cache_namespace_documents(
    *,
    user_id: str,
    namespace: str,
    generation: int,
    documents: dict[str, dict[str, Any]],
) -> None:
    key = (user_id, namespace, generation)
    with _lock:
        _cache[key] = documents
        _cache.move_to_end(key)
        while len(_cache) > _MAX_ENTRIES:
            _cache.popitem(last=False)
