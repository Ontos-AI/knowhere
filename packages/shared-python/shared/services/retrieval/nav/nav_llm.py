"""Boundary LLM adapter for MAP-NAV (Knowhere-aligned call shape).

Nav modules call ``nav_chat`` / ``resolve_nav_model`` only — they must not read
``DS_KEY`` / ``OPENAI_API_KEY`` themselves. Credentials are resolved here via
``resolve_chat_credentials`` (deepseek-* → ``DS_KEY``/``DS_URL``, else
``OPENAI_*``), matching Knowhere's chat credential split (deepseek → DS_*, else OPENAI_*).

Thinking policy (DeepSeek V4 defaults thinking ON if omitted):

- ``action`` (navigate / harvest / refine / verify / score):
  always disabled — short JSON under ``llm_max_tokens`` (often 256).
- ``planner`` (plan_query / replan only): episode-bound
  ``NavConfig.planner_thinking``, else ``NAV_PLANNER_THINKING`` for EXP
  scripts; unset → disabled. When enabled, callers should use
  ``planner_output_max_tokens``. ``plan_control`` uses ``action`` (thinking off).

Migration to Knowhere: inject the production callable with
``set_nav_chat_backend`` (wrap ``llm_fn``); leave nav call sites unchanged.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Dict, Iterator, Optional, Sequence

# Same return shape as ``cached_chat_completion``:
# ``{content, reasoning_content?, usage, cache_hit?}``.
NavChatBackend = Callable[..., Dict[str, Any]]

_backend: Optional[NavChatBackend] = None

_DS_DEFAULT_MODEL = "deepseek-v4-flash"
_DEFAULT_PLANNER_THINK_MAX = 16384

_runtime_planner_thinking: ContextVar[Optional[str]] = ContextVar(
    "nav_runtime_planner_thinking", default=None
)
_runtime_planner_think_max: ContextVar[Optional[int]] = ContextVar(
    "nav_runtime_planner_think_max", default=None
)


def set_nav_chat_backend(fn: Optional[NavChatBackend]) -> None:
    """Install or clear an injected chat backend (tests / Knowhere llm_fn)."""
    global _backend
    _backend = fn


def get_nav_chat_backend() -> Optional[NavChatBackend]:
    return _backend


@contextmanager
def nav_llm_runtime(
    *,
    planner_thinking: str = "",
    planner_think_max_tokens: int = 0,
) -> Iterator[None]:
    """Bind planner thinking knobs for one episode (config values, not process env)."""
    think = str(planner_thinking or "").strip() or None
    think_max = int(planner_think_max_tokens or 0)
    t_tok = _runtime_planner_thinking.set(think)
    m_tok = _runtime_planner_think_max.set(think_max if think_max > 0 else None)
    try:
        yield
    finally:
        _runtime_planner_think_max.reset(m_tok)
        _runtime_planner_thinking.reset(t_tok)


def resolve_nav_model(
    *,
    model: str = "",
    model_env: str = "",
    fallback_envs: Sequence[str] = (),
) -> str:
    """Prefer explicit model string; else env keys (EXP); else deepseek default."""
    preferred = str(model or "").strip()
    if preferred:
        return preferred

    from ._compat import load_llm_env  # type: ignore

    load_llm_env()
    for name in (model_env, *fallback_envs):
        token = str(name or "").strip()
        if not token:
            continue
        val = os.environ.get(token, "").strip()
        if val:
            return val
    return _DS_DEFAULT_MODEL


def resolve_nav_thinking_mode(*, role: str = "action") -> str:
    """Return ``enabled`` or ``disabled`` — never leave API auto (DS default ON)."""
    from ._compat import (  # type: ignore
        load_llm_env,
        resolve_thinking_mode,
    )

    if role != "planner":
        return "disabled"

    bound = _runtime_planner_thinking.get()
    if bound in ("enabled", "disabled"):
        return bound

    load_llm_env()
    mode = resolve_thinking_mode(
        os.environ.get("NAV_PLANNER_THINKING", "").strip() or None
    )
    return mode if mode in ("enabled", "disabled") else "disabled"


def nav_thinking_extra(*, role: str = "action", model: str = "") -> Dict[str, Any]:
    """Build cache-aware thinking ``extra`` for ``nav_chat`` / ``cached_chat_completion``."""
    from ._compat import chat_thinking_extra  # type: ignore

    return chat_thinking_extra(
        mode=resolve_nav_thinking_mode(role=role),
        model=model,
    )


def planner_output_max_tokens(base: int) -> int:
    """Raise planner/control max_tokens when planner thinking is enabled."""
    max_tokens = max(256, int(base or 0))
    if resolve_nav_thinking_mode(role="planner") != "enabled":
        return max_tokens
    bound = _runtime_planner_think_max.get()
    if bound is not None and int(bound) > 0:
        think_max = int(bound)
    else:
        think_max = int(os.environ.get("NAV_PLANNER_THINK_MAX_TOKENS", "").strip() or "0")
        if think_max <= 0:
            think_max = _DEFAULT_PLANNER_THINK_MAX
    return max(max_tokens, think_max)


def _merge_thinking_extra(
    extra: Optional[Dict[str, Any]],
    *,
    role: str,
    model: str,
) -> Dict[str, Any]:
    think = nav_thinking_extra(role=role, model=model)
    merged = dict(extra or {})
    body = dict(merged.get("extra_body") or {})
    body.update(think.get("extra_body") or {})
    merged.update(think)
    if body:
        merged["extra_body"] = body
    return merged


def nav_chat(
    *,
    purpose: str,
    messages: Sequence[Dict[str, str]],
    model: str,
    temperature: float = 0.0,
    max_tokens: Optional[int] = None,
    response_format: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
    thinking_role: str = "action",
    context: str = "Nav",
    api_key_env: str = "",
    base_url_env: str = "",
    timeout: float = 60.0,
    usage_tag: str = "",
) -> Dict[str, Any]:
    """Single entry for nav LLM calls. Credentials stay inside this boundary.

    Token hard-stop: refuse the call when the episode/process budget is already
    exhausted (raises ``NavTokenLimit``). Successful calls credit usage to the
    active episode counter when ``nav_token_episode()`` is entered.
    """
    from .nav_token_budget import (
        NavTokenLimit,
        nav_token_budget_exhausted,
        nav_token_limit,
        nav_tokens_used,
        record_episode_tokens,
    )

    if nav_token_budget_exhausted():
        raise NavTokenLimit(used=nav_tokens_used(), limit=nav_token_limit())

    merged_extra = _merge_thinking_extra(extra, role=thinking_role, model=model)

    if _backend is not None:
        result = _backend(
            purpose=purpose,
            messages=list(messages),
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            extra=merged_extra,
            thinking_role=thinking_role,
            context=context,
            api_key_env=api_key_env,
            base_url_env=base_url_env,
            timeout=timeout,
            usage_tag=usage_tag,
        )
        record_episode_tokens((result or {}).get("usage"))
        return result

    from ._compat import cached_chat_completion  # type: ignore
    from ._compat import (  # type: ignore
        make_openai_client,
        require_llm_env,
        resolve_chat_credentials,
    )
    from ._compat import record_usage  # type: ignore

    require_llm_env(context=context)
    key, base_url = resolve_chat_credentials(
        model=model,
        api_key_env=api_key_env,
        base_url_env=base_url_env,
    )
    if not key:
        raise RuntimeError(
            f"{context}: no LLM credentials after resolve_chat_credentials "
            f"(model={model!r}; need DS_KEY for deepseek-* or OPENAI_API_KEY)."
        )
    client = make_openai_client(api_key=key, base_url=base_url, timeout=timeout)
    cached = cached_chat_completion(
        client,
        purpose=purpose,
        model=model,
        messages=list(messages),
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        extra=merged_extra,
    )
    if usage_tag:
        record_usage(usage_tag, cached.get("usage"))
    record_episode_tokens(cached.get("usage"))
    return cached
