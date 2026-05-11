"""Async LLM adapter for agent-driven retrieval navigation.

Wraps the existing sync OpenAICompatibleClientSync via asyncio.to_thread()
to provide an async callable suitable for the agent navigation pipeline.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, Union, Sequence, cast

from loguru import logger

from shared.core.config import settings

# LLMFn accepts either a plain string or a list of ChatCompletionMessageParam
LLMFnInput = Union[str, Sequence[dict[str, Any]]]
LLMFn = Callable[[LLMFnInput], Coroutine[Any, Any, str]]

_RETRIEVAL_LLM_TEMPERATURE = 0.1
_RETRIEVAL_LLM_MAX_TOKENS = 2048


def _has_llm_credentials() -> bool:
    """Check whether at least one LLM provider is configured."""
    if getattr(settings, 'LLM_MOCK_ENABLED', False):
        return True
    if getattr(settings, 'DS_KEY', ''):
        return True
    if getattr(settings, 'ALI_API_KEYS', ''):
        return True
    if getattr(settings, 'GLM_API_KEY', ''):
        return True
    if getattr(settings, 'GPT_API_KEY', ''):
        return True
    return False


def _resolve_default_model() -> str:
    """Pick a model name that matches the configured LLM provider."""
    if getattr(settings, 'DS_KEY', ''):
        return 'deepseek-chat'
    if getattr(settings, 'ALI_API_KEYS', ''):
        return 'qwen-plus'
    if getattr(settings, 'GLM_API_KEY', ''):
        return 'glm-4-flash'
    if getattr(settings, 'GPT_API_KEY', ''):
        return getattr(settings, 'NORMOL_MODEL', None) or 'gpt-4o-mini'
    return getattr(settings, 'NORMOL_MODEL', None) or 'deepseek-chat'


def create_retrieval_llm_fn(
    *,
    model: str | None = None,
    temperature: float = _RETRIEVAL_LLM_TEMPERATURE,
    max_tokens: int = _RETRIEVAL_LLM_MAX_TOKENS,
) -> LLMFn | None:
    """Create an async LLM callable for retrieval agent navigation.

    Returns None when no LLM provider is configured, signalling the caller
    to fall back to lexical graph routing.
    """
    if not _has_llm_credentials():
        logger.debug('retrieval: no LLM credentials configured, agent navigation disabled')
        return None

    effective_model = model or _resolve_default_model()

    async def llm_fn(prompt: LLMFnInput) -> str:
        from shared.utils.OpenAICompatibleClientSync import get_openai_client

        client = get_openai_client(model=effective_model)
        try:
            result = await asyncio.to_thread(
                client.chat_completion,
                cast(Any, prompt),
                model=effective_model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return result
        except Exception as exc:
            logger.warning(
                "retrieval: agent LLM call failed (degrading gracefully): "
                "model={} error_type={} error={}",
                effective_model,
                type(exc).__name__,
                exc,
            )
            return ''

    return llm_fn
