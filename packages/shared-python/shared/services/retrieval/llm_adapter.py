"""Async LLM adapter for agent-driven retrieval navigation.

Wraps the existing sync OpenAICompatibleClientSync via asyncio.to_thread()
to provide an async callable suitable for the agent navigation pipeline.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any, Callable, Coroutine, Union, Sequence, cast

from loguru import logger

from shared.core.config import settings

# LLMFn accepts either a plain string or a list of ChatCompletionMessageParam
LLMFnInput = Union[str, Sequence[dict[str, Any]]]
LLMFn = Callable[[LLMFnInput], Coroutine[Any, Any, str]]
LLMUsage = dict[str, int]
current_llm_usage: ContextVar[LLMUsage | None] = ContextVar(
    'current_llm_usage',
    default=None,
)

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


def _resolve_planner_model(*, thinking: bool) -> str:
    configured = getattr(settings, 'RETRIEVAL_PLANNER_MODEL', '') or ''
    if configured:
        return configured
    if getattr(settings, 'DS_KEY', ''):
        return 'deepseek-reasoner' if thinking else 'deepseek-chat'
    if getattr(settings, 'ALI_API_KEYS', ''):
        return 'qwq-32b-preview' if thinking else 'qwen-plus'
    if getattr(settings, 'GLM_API_KEY', ''):
        return 'glm-4-plus' if thinking else 'glm-4-flash'
    if getattr(settings, 'GPT_API_KEY', ''):
        return 'o3-mini' if thinking else 'gpt-4o-mini'
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
        current_llm_usage.set(None)
        result, usage = await asyncio.to_thread(
            client.chat_completion_with_usage,
            cast(Any, prompt),
            model=effective_model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        current_llm_usage.set(usage)
        return result

    return llm_fn


def create_retrieval_planner_fn(
    *,
    thinking: bool = True,
    model: str | None = None,
    max_tokens: int = 8192,
) -> LLMFn | None:
    """Create a reasoning-capable LLM callable for query planning."""
    if not _has_llm_credentials():
        logger.debug('retrieval: no LLM credentials configured, workflow planner disabled')
        return None

    effective_model = model or _resolve_planner_model(thinking=thinking)

    async def llm_fn(prompt: LLMFnInput) -> str:
        from shared.utils.OpenAICompatibleClientSync import get_openai_client

        client = get_openai_client(model=effective_model)
        current_llm_usage.set(None)
        result, usage = await asyncio.to_thread(
            client.chat_completion_with_usage,
            cast(Any, prompt),
            model=effective_model,
            temperature=0.0,
            max_tokens=max_tokens,
        )
        current_llm_usage.set(usage)
        return result

    return llm_fn


def create_retrieval_vlm_fn(
    *,
    model: str | None = None,
    temperature: float = _RETRIEVAL_LLM_TEMPERATURE,
    max_tokens: int = 4096,
) -> LLMFn | None:
    """Create an async VLM callable for image-aware answer generation.

    Uses the IMAGE_MODEL (e.g. qwen3.5-flash) for multimodal input.
    Returns None when the image model is not configured.

    The returned function accepts the same ``LLMFnInput`` type as
    ``create_retrieval_llm_fn`` — callers pass either a plain string
    or a list of ChatCompletionMessageParam (including image_url parts).
    """
    from shared.core.config import settings

    effective_model = model or getattr(settings, 'IMAGE_MODEL', '') or 'qwen3.5-flash'

    if not _has_llm_credentials():
        logger.debug('retrieval: no LLM credentials for VLM, image-aware answering disabled')
        return None

    async def vlm_fn(prompt: LLMFnInput) -> str:
        from shared.utils.OpenAICompatibleClientSync import get_openai_client

        client = get_openai_client(model=effective_model)
        current_llm_usage.set(None)
        result, usage = await asyncio.to_thread(
            client.chat_completion_with_usage,
            cast(Any, prompt),
            model=effective_model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        current_llm_usage.set(usage)
        return result

    return vlm_fn
