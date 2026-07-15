"""Request-scoped BYOK LLM credential overrides.

In the gevent worker, child greenlets (GeventPool.spawn) do NOT inherit
``ContextVar`` or ``threading.local`` from the parent.  We therefore use
a module-level dict keyed by the *root* greenlet id of the current parse
task — the same pattern as ``token_tracking``.

For the asyncio retrieval path a ``ContextVar`` is used instead, which
propagates naturally across ``await`` and ``asyncio.to_thread``.

``get_current_llm_overrides()`` checks the greenlet dict first, then the
ContextVar, so both runtimes share one lookup API.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar, Token
from typing import Any, Optional

from shared.models.schemas.llm_config import LLMConfig, LLMProviderConfig, parse_llm_config

# Greenlet-root keyed store (worker / gevent).
_overrides: dict[int, LLMConfig] = {}
_root_ids: dict[int, int] = {}
_lock = threading.Lock()

# Async ContextVar store (API retrieval).
_async_overrides: ContextVar[LLMConfig | None] = ContextVar(
    "llm_overrides_async",
    default=None,
)

ResolvedCredentials = tuple[str | None, str | None, str | None]
# (model, api_key, api_url)


def _current_greenlet_id() -> int:
    try:
        import gevent

        return id(gevent.getcurrent())
    except ImportError:
        return threading.get_ident()


def _find_root_id() -> int | None:
    """Walk up the greenlet parent chain to find a registered root id."""
    gid = _current_greenlet_id()
    if gid in _overrides:
        return gid
    if gid in _root_ids:
        return _root_ids[gid]
    try:
        import gevent

        g = gevent.getcurrent()
        while g is not None:
            pid = id(g)
            if pid in _overrides:
                _root_ids[gid] = pid
                return pid
            g = getattr(g, "parent", None)
    except ImportError:
        pass
    return None


def init_llm_overrides(config: LLMConfig | dict[str, Any] | None) -> LLMConfig | None:
    """Register BYOK overrides for the current parse-task root greenlet.

    Must be called from the root greenlet of the task (same scope as
    ``init_token_tracker``).  Returns the parsed config, or None when
    no override is active.
    """
    parsed = parse_llm_config(config)
    if parsed is None:
        return None
    gid = _current_greenlet_id()
    with _lock:
        _overrides[gid] = parsed
    return parsed


def cleanup_llm_overrides() -> None:
    """Remove the override for the current greenlet. Call after parsing."""
    gid = _current_greenlet_id()
    with _lock:
        _overrides.pop(gid, None)
        stale = [k for k, v in _root_ids.items() if v == gid]
        for k in stale:
            del _root_ids[k]


def set_llm_overrides_async(
    config: LLMConfig | dict[str, Any] | None,
) -> Token[LLMConfig | None]:
    """Set BYOK overrides for the current asyncio task. Returns a reset token."""
    parsed = parse_llm_config(config)
    return _async_overrides.set(parsed)


def reset_llm_overrides_async(token: Token[LLMConfig | None]) -> None:
    """Reset the async override ContextVar using the token from set_*."""
    _async_overrides.reset(token)


def get_current_llm_overrides() -> LLMConfig | None:
    """Return the active BYOK config for this task, if any.

    Checks the gevent greenlet-root store first, then the async ContextVar.
    """
    root = _find_root_id()
    if root is not None:
        return _overrides.get(root)
    return _async_overrides.get()


def _resolve_provider(
    provider: LLMProviderConfig | None,
    requested_model: str | None,
) -> ResolvedCredentials:
    if provider is None:
        return (requested_model, None, None)
    return (provider.model, provider.api_key, provider.base_url)


def resolve_text(requested_model: str | None = None) -> ResolvedCredentials:
    """Resolve (model, api_key, api_url) for a text LLM call."""
    overrides = get_current_llm_overrides()
    if overrides is None:
        return (requested_model, None, None)
    return _resolve_provider(overrides.text_effective(), requested_model)


def resolve_vision(requested_model: str | None = None) -> ResolvedCredentials:
    """Resolve (model, api_key, api_url) for a vision/VLM call."""
    overrides = get_current_llm_overrides()
    if overrides is None:
        return (requested_model, None, None)
    return _resolve_provider(overrides.vision_effective(), requested_model)


def get_text_client(requested_model: Optional[str] = None):
    """Return ``(client, effective_model)`` for a text LLM call."""
    from shared.services.ai.openai_compatible_client_sync import get_openai_client

    model, api_key, api_url = resolve_text(requested_model)
    client = get_openai_client(model=model, api_key=api_key, api_url=api_url)
    return client, model


def get_vision_client(requested_model: Optional[str] = None):
    """Return ``(client, effective_model)`` for a vision/VLM call."""
    from shared.services.ai.openai_compatible_client_sync import get_openai_client

    model, api_key, api_url = resolve_vision(requested_model)
    client = get_openai_client(model=model, api_key=api_key, api_url=api_url)
    return client, model
