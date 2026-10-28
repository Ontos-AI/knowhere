from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

from shared.services.retrieval.routing import config as routing_config
from shared.services.retrieval.routing.prompt import QUERY_ROUTER_SYSTEM_PROMPT
from shared.services.retrieval.routing.types import QueryMode, RouterDecision


def _resolve_client_and_model() -> tuple[Any, str]:
    from shared.services.ai.llm_overrides import resolve_text
    from shared.services.ai.openai_compatible_client_sync import get_openai_client

    requested = routing_config.QUERY_ROUTER_MODEL
    effective_model, api_key, api_url = resolve_text(requested)
    model = effective_model or requested
    client = get_openai_client(model=model, api_key=api_key, api_url=api_url)
    return client, model


def _fallback_decision(*, model_name: str, tokens_used: int = 0) -> RouterDecision:
    return RouterDecision(
        mode=QueryMode.AGENT,
        reason="",
        source="fallback",
        model_name=model_name,
        tokens_used=tokens_used,
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


async def classify_query_mode(query: str) -> RouterDecision:
    model_name = ""
    try:
        client, model_name = _resolve_client_and_model()
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
        tokens_used = int((usage or {}).get("total_tokens", 0) or 0)
        return _parse_decision(
            str(content or ""),
            model_name=model_name,
            tokens_used=tokens_used,
        )
    except Exception as exc:
        logger.warning("retrieval query router fallback to agent: {}", exc)
        return _fallback_decision(model_name=model_name)
