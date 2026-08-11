"""Sync ``nav_chat`` backend for Knowhere map-nav.

Install once at import (module-level). Forwards ``extra`` / thinking as-is.
Model defaults for empty ``model=`` come from ``nav_config.MAPNAV_MODEL``.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Sequence

from shared.services.retrieval.nav.nav_llm import set_nav_chat_backend
from shared.services.retrieval.nav_config import MAPNAV_MODEL


def nav_chat_sync_backend(
    *,
    messages: Sequence[Dict[str, Any]],
    model: str = "",
    temperature: float = 0.0,
    max_tokens: Optional[int] = None,
    response_format: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
    timeout: float = 60.0,
    **_kwargs: Any,
) -> Dict[str, Any]:
    """Knowhere sync backend matching ``nav_chat``'s injected callable shape."""
    from shared.services.ai.llm_overrides import resolve_text
    from shared.services.ai.openai_compatible_client_sync import get_openai_client

    requested = str(model or "").strip() or MAPNAV_MODEL
    effective_model, api_key, api_url = resolve_text(requested)
    client = get_openai_client(
        model=effective_model or requested,
        api_key=api_key,
        api_url=api_url,
    )

    call_kwargs: Dict[str, Any] = {}
    if response_format is not None:
        call_kwargs["response_format"] = response_format

    if isinstance(extra, dict):
        body = extra.get("extra_body")
        if isinstance(body, dict) and body:
            call_kwargs["extra_body"] = dict(body)

    timeout_arg: Optional[int] = None
    if timeout is not None and math.isfinite(float(timeout)):
        timeout_arg = max(1, int(math.ceil(float(timeout))))

    text, usage = client.chat_completion_with_usage(
        list(messages),
        model=effective_model or requested,
        temperature=float(temperature if temperature is not None else 0.0),
        max_tokens=int(max_tokens) if max_tokens is not None else 2048,
        timeout=timeout_arg,
        **call_kwargs,
    )
    return {"content": text or "", "usage": usage or {}}


def install_nav_chat_backend() -> None:
    """Register the sync backend once (safe to call repeatedly)."""
    set_nav_chat_backend(nav_chat_sync_backend)


install_nav_chat_backend()
