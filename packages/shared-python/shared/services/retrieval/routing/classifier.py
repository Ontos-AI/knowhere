from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Literal

import httpx
from loguru import logger

from shared.services.retrieval.routing import config as routing_config
from shared.services.retrieval.routing.prompt import QUERY_ROUTER_SYSTEM_PROMPT
from shared.services.retrieval.routing.types import QueryMode, RouterDecision


def _fallback_decision(
    *, model_name: str, tokens_used: int = 0, failure_stage: str = "model_response"
) -> RouterDecision:
    return RouterDecision(
        mode=QueryMode.AGENT,
        reason="",
        source="fallback",
        model_name=model_name,
        tokens_used=tokens_used,
        failure_stage=failure_stage,
    )


def _parse_decision(
    content: str, *, model_name: str, tokens_used: int
) -> RouterDecision:
    try:
        parsed = json.loads(content)
    except ValueError:
        return _fallback_decision(model_name=model_name, tokens_used=tokens_used)
    if not isinstance(parsed, dict):
        return _fallback_decision(model_name=model_name, tokens_used=tokens_used)
    mode_raw = str(parsed.get("mode") or "").strip().lower()
    if mode_raw not in {QueryMode.AGENT.value, QueryMode.CLASSIC.value}:
        return _fallback_decision(model_name=model_name, tokens_used=tokens_used)
    return RouterDecision(
        mode=QueryMode(mode_raw),
        reason=str(parsed.get("reason") or ""),
        source="llm",
        model_name=model_name,
        tokens_used=tokens_used,
    )


def _describe_failure(error: Exception, stage: str) -> tuple[str, str, int | None]:
    error_type: str = type(error).__name__
    error_number: int | None = None
    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            break
        if isinstance(current, httpx.HTTPError):
            error_type = type(current).__name__
            if isinstance(current, (httpx.ConnectError, httpx.ConnectTimeout)):
                stage = "model_connect"
            elif isinstance(current, httpx.UnsupportedProtocol):
                stage = "model_protocol"
            elif isinstance(
                current, (httpx.HTTPStatusError, httpx.ReadError, httpx.ReadTimeout)
            ):
                stage = "model_response"
        if isinstance(current, OSError) and current.errno is not None:
            error_number = current.errno
        current = current.__cause__ or current.__context__
    return stage, error_type, error_number


async def classify_query_mode(query: str) -> RouterDecision:
    model_name: str = ""
    attempted_model_name: str = routing_config.QUERY_ROUTER_MODEL
    model_config_source: Literal["default", "request"] | None = None
    failure_stage: str = "model_config"
    try:
        from shared.services.ai.llm_overrides import resolve_text
        from shared.services.ai.openai_compatible_client_sync import get_openai_client

        effective_model, api_key, api_url = resolve_text(attempted_model_name)
        attempted_model_name = effective_model or attempted_model_name
        model_config_source = "request" if api_url is not None else "default"
        client = get_openai_client(
            model=attempted_model_name, api_key=api_key, api_url=api_url
        )
        model_name = attempted_model_name
        failure_stage = "model_request"
        content, usage = await asyncio.to_thread(
            client.chat_completion_with_usage,
            [
                {"role": "system", "content": QUERY_ROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            model=model_name,
            temperature=0.0,
            max_tokens=routing_config.QUERY_ROUTER_MAX_TOKENS,
            timeout=routing_config.QUERY_ROUTER_TIMEOUT_SECONDS,
            response_format={"type": "json_object"},
            usage_task="retrieval.query_router",
        )
        failure_stage = "model_response"
        tokens_used = int((usage or {}).get("total_tokens", 0) or 0)
        decision: RouterDecision = _parse_decision(
            str(content or ""),
            model_name=model_name,
            tokens_used=tokens_used,
        )
    except Exception as exc:
        failure_stage, error_type, error_number = _describe_failure(exc, failure_stage)
        logger.warning("retrieval query router fallback to agent: {}", error_type)
        decision = replace(
            _fallback_decision(model_name=model_name, failure_stage=failure_stage),
            attempted_model_name=attempted_model_name,
            failure_error_type=error_type,
            failure_errno=error_number,
        )
    return replace(decision, model_config_source=model_config_source)
