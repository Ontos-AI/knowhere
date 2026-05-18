"""
Synchronous OpenAI-compatible client for gevent worker tasks.
Uses the official OpenAI SDK with shared httpx connection pooling
and built-in 429 retry with exponential backoff.

For Aliyun (qwen) models, api keys are drawn from the AliQuotaManager
token pool and automatically rotated on 429 RateLimitError.
"""
import os
import threading
from typing import Any, Dict, List, Optional, Union

import openai
from loguru import logger
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import LLMServiceException
from shared.services.http.client_pool import get_sync_client
from shared.services.ai.llm_mock import build_mock_chat_completion_response
from shared.core.security import mask_api_key

LOCAL_DEBUG = os.getenv("LOCAL_DEBUG", "0") == "1"
LLMUsage = dict[str, int]

_client_cache: Dict[tuple, "OpenAICompatibleClientSync"] = {}
_client_cache_lock = threading.Lock()


def _is_ali_model(model_name: str) -> bool:
    """Check whether a model name routes to Aliyun DashScope."""
    return "qwen" in (model_name or "").lower()


def _should_mock_llm_calls() -> bool:
    """Whether all OpenAI-compatible LLM calls should short-circuit to mock responses."""
    return bool(getattr(settings, "LLM_MOCK_ENABLED", False))


def _empty_usage() -> LLMUsage:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _extract_usage(response: Any) -> LLMUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return _empty_usage()
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def _summarize_exception_chain(exc: Exception, *, max_depth: int = 4) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()

    while current is not None and len(parts) < max_depth and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__

    return " <- ".join(parts)


class OpenAICompatibleClientSync:
    """Sync OpenAI-compatible client backed by the official OpenAI SDK."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        default_model: Optional[str] = None,
        timeout: int = 300,
        max_retries: int = 2,
    ):
        self.default_model = default_model or getattr(settings, "NORMOL_MODEL", "deepseek-chat")
        self._explicit_api_key = api_key
        self._explicit_api_url = api_url
        self._max_retries = max_retries
        self._base_url: Optional[str] = self._resolve_base_url(
            model_name=self.default_model,
            api_url=api_url,
        )

        self.timeout = (
            getattr(settings, "OPENAI_CLIENT_TIMEOUT", 300)
            if timeout is None
            else timeout
        )
        self._client: Optional[OpenAI] = None
        if _should_mock_llm_calls():
            return
        if not self._should_use_ali_pool():
            resolved_key: Optional[str] = self._resolve_direct_api_key(
                model_name=self.default_model,
                api_key=api_key,
            )
            self._client = self._build_client(
                api_key=resolved_key,
                base_url=self._base_url,
                max_retries=max_retries,
            )

    def _build_client(
        self,
        api_key: Optional[str],
        base_url: Optional[str],
        max_retries: int,
    ) -> OpenAI:
        """Build a direct OpenAI-compatible SDK client."""
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=get_sync_client(),
            max_retries=max_retries,
            timeout=self.timeout,
        )

    @staticmethod
    def _strip_chat_completions(url: Optional[str]) -> Optional[str]:
        """Strip /chat/completions suffix so the OpenAI SDK can append it."""
        if url and url.rstrip("/").endswith("/chat/completions"):
            return url.rstrip("/").removesuffix("/chat/completions")
        return url

    def _resolve_base_url(
        self,
        model_name: str,
        api_url: Optional[str],
    ) -> Optional[str]:
        if api_url:
            return self._strip_chat_completions(api_url)

        model_lower = (model_name or "").lower()
        if "qwen" in model_lower:
            ali_base = getattr(settings, "ALI_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
            return self._strip_chat_completions(ali_base)

        if "glm" in model_lower:
            glm_base = getattr(settings, "GLM_URL", "https://open.bigmodel.cn/api/paas/v4")
            return self._strip_chat_completions(glm_base)

        if "doubao" in model_lower or model_lower.startswith("ep-"):
            return self._strip_chat_completions(getattr(settings, "ARK_URL", None))

        return self._strip_chat_completions(settings.DS_URL)

    def _resolve_direct_api_key(
        self,
        model_name: str,
        api_key: Optional[str],
    ) -> Optional[str]:
        if api_key:
            return api_key

        model_lower = (model_name or "").lower()
        if "glm" in model_lower:
            return getattr(settings, "GLM_API_KEY", None)

        if "doubao" in model_lower or model_lower.startswith("ep-"):
            return getattr(settings, "ARK_API_KEY", None)

        return settings.DS_KEY

    # ------------------------------------------------------------------
    # Ali token-pool helpers
    # ------------------------------------------------------------------

    def _should_use_ali_pool(self) -> bool:
        """Whether to route through the AliQuotaManager instead of a fixed key."""
        if self._explicit_api_key:
            return False
        return _is_ali_model(self.default_model)

    def _make_ali_pool_call(
        self,
        model: str,
        all_messages: List[ChatCompletionMessageParam],
        temperature: float,
        max_tokens: int,
        api_kwargs: Dict[str, Any],
    ) -> tuple[str, LLMUsage]:
        """Acquire a token, make the call, and retry inline on 429."""
        from shared.services.ai.ali_quota_manager import get_ali_quota_manager

        quota_manager = get_ali_quota_manager()
        base_url: Optional[str] = self._base_url

        max_retries = settings.ALI_INLINE_MAX_RETRIES
        for attempt in range(max_retries):
            lease = quota_manager.acquire_request(operation="chat_completion")
            try:
                # Hybrid rate-limit control: SDK handles per-key backoff (exp. backoff + jitter),
                # outer loop handles token rotation across the pool on persistent 429s.
                client = self._build_client(
                    api_key=lease.api_key,
                    base_url=base_url,
                    max_retries=settings.ALI_SDK_MAX_RETRIES,
                )
                response = client.chat.completions.create(
                    model=model,
                    messages=all_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **api_kwargs,
                )
                choices = response.choices
                if not choices:
                    raise LLMServiceException(
                        internal_message="AI returned empty result",
                        provider=self.default_model,
                    )
                return choices[0].message.content or "", _extract_usage(response)
            except openai.RateLimitError as exc:
                retry_after = _parse_retry_after(exc)
                quota_manager.mark_rate_limited(lease.token_id, retry_after)
                logger.warning(
                    f"Ali token {lease.token_id} rate-limited (attempt {attempt + 1}/{max_retries}), "
                    f"cooling down {retry_after}s and retrying with next token"
                )
                if attempt == max_retries - 1:
                    raise LLMServiceException(
                        internal_message=f"All Ali tokens exhausted after {max_retries} retries: {exc}",
                        provider=self.default_model,
                        original_exception=exc,
                    ) from exc
            except LLMServiceException:
                raise
            except Exception as exc:
                masked_api_key = mask_api_key(lease.api_key)
                logger.error(
                    "LLM request failed (Ali pool): model={model}, token_id={token_id}, api_key={api_key}, error={error}",
                    model=model,
                    token_id=lease.token_id,
                    api_key=masked_api_key,
                    error=exc,
                )
                raise LLMServiceException(
                    internal_message=(
                        "API request failed "
                        f"(Ali pool, token_id={lease.token_id}, api_key={masked_api_key}): {exc}"
                    ),
                    provider=self.default_model,
                    original_exception=exc,
                ) from exc
        # unreachable but satisfies the type checker
        raise LLMServiceException(
            internal_message="Ali token pool retry loop exited unexpectedly",
            provider=self.default_model,
        )

    # ------------------------------------------------------------------

    def chat_completion_with_usage(
        self,
        messages: Union[str, List[ChatCompletionMessageParam]],
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        top_p: Optional[float] = None,
        timeout: Optional[int] = None,
        **kwargs,
    ) -> tuple[str, LLMUsage]:
        all_messages: List[ChatCompletionMessageParam]
        if isinstance(messages, list):
            all_messages = messages  # type: ignore[assignment]
        else:
            all_messages = [{"role": "user", "content": str(messages)}]

        api_kwargs: Dict[str, Any] = {}
        if top_p is not None:
            api_kwargs["top_p"] = top_p
        if timeout is not None:
            api_kwargs["timeout"] = timeout

        allowed_api_params = {
            "n", "stop", "presence_penalty", "frequency_penalty",
            "logit_bias", "user", "seed", "tools", "tool_choice",
            "response_format", "logprobs", "top_logprobs",
        }
        for key, value in kwargs.items():
            if key in allowed_api_params:
                api_kwargs[key] = value

        # ── disable thinking mode ──
        # Qwen3.5 by default enables thinking mode, which wastes tokens by outputting <think>...</think>
        # Explicitly disable it in all API calls
        extra_body = api_kwargs.get("extra_body", {})
        if isinstance(extra_body, dict):
            extra_body.setdefault("enable_thinking", False)
        else:
            extra_body = {"enable_thinking": False}
        api_kwargs["extra_body"] = extra_body

        effective_model = model or self.default_model
        if _should_mock_llm_calls():
            return build_mock_chat_completion_response(
                messages=all_messages,
                model_name=effective_model,
            ), _empty_usage()

        # Route through Ali token pool when applicable
        if self._should_use_ali_pool():
            try:
                return self._make_ali_pool_call(
                    model=effective_model,
                    all_messages=all_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    api_kwargs=api_kwargs,
                )
            except LLMServiceException:
                raise
            except Exception as exc:
                logger.error(f"LLM request failed (Ali pool): model={effective_model}, error={exc}")
                raise LLMServiceException(
                    internal_message=f"API request failed: {str(exc)}",
                    provider=self.default_model,
                    original_exception=exc,
                ) from exc

        # Non-Ali path: use the single pre-configured client
        client = self._client
        if client is None:
            raise LLMServiceException(
                internal_message="OpenAI client is not initialized for direct provider requests",
                provider=self.default_model,
            )

        try:
            response = client.chat.completions.create(
                model=effective_model,
                messages=all_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **api_kwargs,
            )

            choices = response.choices
            if not choices:
                raise LLMServiceException(
                    internal_message="AI returned empty result",
                    provider=self.default_model,
                )

            content = choices[0].message.content or ""
            return content, _extract_usage(response)
        except LLMServiceException:
            raise
        except Exception as exc:
            logger.error(
                "LLM request failed: model={model}, base_url={base_url}, error_chain={error_chain}",
                model=effective_model,
                base_url=client.base_url,
                error_chain=_summarize_exception_chain(exc),
            )
            raise LLMServiceException(
                internal_message=f"API request failed: {_summarize_exception_chain(exc)}",
                provider=self.default_model,
                original_exception=exc,
            ) from exc

    def chat_completion(
        self,
        messages: Union[str, List[ChatCompletionMessageParam]],
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        top_p: Optional[float] = None,
        timeout: Optional[int] = None,
        **kwargs,
    ) -> str:
        content, _usage = self.chat_completion_with_usage(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            timeout=timeout,
            **kwargs,
        )
        return content


def _parse_retry_after(exc: openai.RateLimitError) -> int:
    """Extract Retry-After seconds from a RateLimitError, with sane bounds."""
    try:
        if hasattr(exc, "response") and exc.response is not None:
            header_value = exc.response.headers.get("retry-after") or exc.response.headers.get("Retry-After")
            if header_value:
                return max(1, min(int(header_value), 120))
    except (ValueError, TypeError, AttributeError):
        pass
    return settings.ALI_TOKEN_COOLDOWN_SECONDS


def get_openai_client(
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    timeout: int = 300,
    max_retries: int = 2,
) -> OpenAICompatibleClientSync:
    """Return a cached OpenAICompatibleClientSync instance keyed by config."""
    cache_key = (model, api_key, api_url, timeout, max_retries)
    client = _client_cache.get(cache_key)
    if client is not None:
        return client
    with _client_cache_lock:
        client = _client_cache.get(cache_key)
        if client is not None:
            return client
        client = OpenAICompatibleClientSync(
            api_key=api_key,
            api_url=api_url,
            default_model=model,
            timeout=timeout,
            max_retries=max_retries,
        )
        _client_cache[cache_key] = client
        return client
