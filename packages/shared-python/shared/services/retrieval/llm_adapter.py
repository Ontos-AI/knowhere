"""Async LLM helpers retained for non-mapnav call sites.

Map-nav uses ``nav_llm_backend`` + ``OpenAICompatibleClientSync`` instead.
"""
from __future__ import annotations

import asyncio
import math
from contextvars import ContextVar
from typing import Any, Callable, Coroutine, Union, Sequence, cast

from loguru import logger

from shared.core.config import settings
from shared.services.ai.llm_overrides import get_current_llm_overrides

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
    if get_current_llm_overrides() is not None:
        return True
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
    overrides = get_current_llm_overrides()
    if overrides is not None:
        provider = overrides.text_effective()
        if provider is not None:
            return provider.model
    if getattr(settings, 'DS_KEY', ''):
        return 'deepseek-v4-flash'
    if getattr(settings, 'ALI_API_KEYS', ''):
        return 'qwen-plus'
    if getattr(settings, 'GLM_API_KEY', ''):
        return 'glm-4-flash'
    if getattr(settings, 'GPT_API_KEY', ''):
        return getattr(settings, 'NORMOL_MODEL', None) or 'gpt-4o-mini'
    return getattr(settings, 'NORMOL_MODEL', None) or 'deepseek-v4-flash'


def _resolve_vlm_model(model: str | None = None) -> str:
    overrides = get_current_llm_overrides()
    if overrides is not None:
        provider = overrides.vision_effective()
        if provider is not None:
            return provider.model
    return model or getattr(settings, 'IMAGE_MODEL', '') or 'qwen3.6-flash'


def _build_client_for_channel(*, channel: str, model: str):
    """Build an OpenAI-compatible client, honoring active BYOK overrides."""
    from shared.services.ai.openai_compatible_client_sync import get_openai_client
    from shared.services.ai.llm_overrides import resolve_text, resolve_vision

    resolve = resolve_vision if channel == 'vision' else resolve_text
    effective_model, api_key, api_url = resolve(model)
    return get_openai_client(
        model=effective_model,
        api_key=api_key,
        api_url=api_url,
    ), effective_model


def create_retrieval_llm_fn(
    *,
    model: str | None = None,
    temperature: float = _RETRIEVAL_LLM_TEMPERATURE,
    max_tokens: int = _RETRIEVAL_LLM_MAX_TOKENS,
) -> LLMFn | None:
    """Create an async LLM callable (legacy helper; map-nav uses nav_llm_backend).

    Returns None when no LLM provider is configured.
    """
    if not _has_llm_credentials():
        logger.debug('retrieval: no LLM credentials configured')
        return None

    effective_model = model or _resolve_default_model()

    async def llm_fn(prompt: LLMFnInput) -> str:
        client, resolved_model = _build_client_for_channel(
            channel='text',
            model=effective_model,
        )
        current_llm_usage.set(None)

        result, usage = await asyncio.to_thread(
            client.chat_completion_with_usage,
            cast(Any, prompt),
            model=resolved_model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        current_llm_usage.set(usage)
        return result

    return llm_fn


def _coerce_provider_timeout_seconds(timeout_seconds: float | None) -> int | None:
    if timeout_seconds is None or not math.isfinite(timeout_seconds):
        return None
    return max(1, math.ceil(timeout_seconds))


def create_retrieval_vlm_fn(
    *,
    model: str | None = None,
    temperature: float = _RETRIEVAL_LLM_TEMPERATURE,
    max_tokens: int = 4096,
) -> LLMFn | None:
    """Create an async VLM callable for image-aware answer generation.

    Uses the IMAGE_MODEL (e.g. qwen3.6-flash) for multimodal input.
    Returns None when the image model is not configured.

    The returned function accepts the same ``LLMFnInput`` type as
    ``create_retrieval_llm_fn`` — callers pass either a plain string
    or a list of ChatCompletionMessageParam (including image_url parts).
    """
    effective_model = _resolve_vlm_model(model)

    if not _has_llm_credentials():
        logger.debug('retrieval: no LLM credentials for VLM, image-aware answering disabled')
        return None

    async def vlm_fn(prompt: LLMFnInput) -> str:
        client, resolved_model = _build_client_for_channel(
            channel='vision',
            model=effective_model,
        )
        current_llm_usage.set(None)
        result, usage = await asyncio.to_thread(
            client.chat_completion_with_usage,
            cast(Any, prompt),
            model=resolved_model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        current_llm_usage.set(usage)
        return result

    return vlm_fn
