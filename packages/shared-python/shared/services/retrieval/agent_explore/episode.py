"""In-process tool-calling episode over the ``agent_tools`` corpus registry.

Uses ``OpenAICompatibleClientSync.chat_completion_raw_with_usage`` — the RAW
response, not ``chat_completion_with_usage`` — because the latter only
returns ``.content`` and silently drops ``message.tool_calls``. Verified live
(2026-09-08) against ``deepseek-v4-flash`` via this codebase's client:
single tool call, parallel tool calls in one turn, tool-result feedback +
final synthesis, and forced ``tool_choice`` (used for the budget-exhaustion
cutoff below) all work.

Tool calls within one turn are dispatched sequentially against the single
shared ``ToolContext.db`` (``AsyncSession``), not via ``asyncio.gather``:
SQLAlchemy's ``AsyncSession`` is not safe for concurrent use from multiple
coroutines. Batching several tool calls into one LLM turn already removes
the LLM round-trip per tool (the dominant cost); true DB-level concurrency
within a turn is not implemented here.

No import from ``nav/`` or ``nav_config.py`` — see ``config.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_explore.config import (
    AGENT_EXPLORE_MAX_COMPLETION_TOKENS,
    AGENT_EXPLORE_MODEL,
    FINISH_TOOL_DESCRIPTION,
    FINISH_TOOL_NAME,
    FINISH_TOOL_SCHEMA,
    LOOP_CONTRACT_SUFFIX,
)
from shared.services.retrieval.agent_explore.types import AgentStep, EpisodeResult
from shared.services.retrieval.agent_tools import (
    REGISTRY,
    ToolContext,
    ToolResult,
    load_corpus_schema_text,
)
from shared.services.retrieval.agent_tools import tools as _agent_tools_registered  # noqa: F401


def _resolve_client_and_model() -> tuple[Any, str]:
    """Mirrors ``nav_llm_backend.nav_chat_sync_backend``'s resolve pattern,
    pinned to ``AGENT_EXPLORE_MODEL`` instead of ``nav_config.MAPNAV_MODEL``.
    """
    from shared.services.ai.llm_overrides import resolve_text
    from shared.services.ai.openai_compatible_client_sync import get_openai_client

    requested = AGENT_EXPLORE_MODEL
    effective_model, api_key, api_url = resolve_text(requested)
    model = effective_model or requested
    client = get_openai_client(model=model, api_key=api_key, api_url=api_url)
    return client, model


def _openai_safe_name(name: str) -> str:
    """DeepSeek's (OpenAI-compatible) function-calling API rejects ``.`` in
    ``tools[].function.name`` (must match ``^[a-zA-Z0-9_-]+$``, verified
    live), but every ``agent_tools`` name is dotted (``corpus.read``) and
    that's also what MCP clients see (no such restriction there) — so the
    dotted name stays canonical in ``REGISTRY``/MCP, and this harness-local
    underscore form exists only for the wire format to this one provider.
    """
    return name.replace(".", "_")


def _build_openai_tools() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return ``(tools, name_map)`` where ``name_map`` maps the OpenAI-safe
    name back to the canonical ``REGISTRY`` name (``finish`` maps to itself).
    """
    name_map: dict[str, str] = {FINISH_TOOL_NAME: FINISH_TOOL_NAME}
    tools: list[dict[str, Any]] = []
    for spec in REGISTRY.all():
        safe_name = _openai_safe_name(spec.name)
        name_map[safe_name] = spec.name
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": safe_name,
                    "description": spec.description,
                    "parameters": spec.json_schema,
                },
            }
        )
    tools.append(
        {
            "type": "function",
            "function": {
                "name": FINISH_TOOL_NAME,
                "description": FINISH_TOOL_DESCRIPTION,
                "parameters": FINISH_TOOL_SCHEMA,
            },
        }
    )
    return tools, name_map


def _safe_json_loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_finish_refs(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict) and str(item.get("document_id") or "").strip():
            normalized.append(item)
    return normalized


def _tool_message_content(result: ToolResult, *, max_chars: int) -> str:
    """Cap a tool's rendered text before it enters LLM context.

    Uses ``ToolBudget.max_chars`` (``EVIDENCE_TEXT_CHAR_BUDGET``, aligned with
    map-nav evidence packing) so tools like ``read`` can return unbounded body
    text while the harness still bounds what the model sees per turn.
    """
    if result.error:
        return f"error: {result.error}"
    text = result.text or "(empty result)"
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return (
        text[:max_chars]
        + f"\n...[truncated, {omitted} more chars — call corpus.read again "
        "with a narrower/more specific ref if you need the rest]"
    )


async def run_agent_explore_episode(
    *,
    db: AsyncSession,
    user_id: str,
    namespace: str,
    query: str,
    budget: EpisodeBudget | None = None,
) -> EpisodeResult:
    budget = budget or EpisodeBudget()
    tool_ctx = ToolContext(db=db, user_id=user_id, namespace=namespace)
    client, model = _resolve_client_and_model()
    openai_tools, tool_name_map = _build_openai_tools()

    system_prompt = load_corpus_schema_text() + LOOP_CONTRACT_SUFFIX
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query},
    ]

    steps: list[AgentStep] = []
    stop_reason = "finished"
    result_refs: list[dict[str, Any]] = []
    result_notes = ""

    while True:
        forced_reason = budget.exhausted()
        tool_choice: Any = "auto"
        if forced_reason is not None:
            tool_choice = {"type": "function", "function": {"name": FINISH_TOOL_NAME}}

        turn_started = time.perf_counter()
        response, usage = await asyncio.to_thread(
            client.chat_completion_raw_with_usage,
            messages=messages,
            model=model,
            temperature=0.0,
            max_tokens=AGENT_EXPLORE_MAX_COMPLETION_TOKENS,
            tools=openai_tools,
            tool_choice=tool_choice,
        )
        budget.record_usage(usage)
        budget.record_step()
        turn_elapsed_ms = int((time.perf_counter() - turn_started) * 1000)
        turn_tokens = int((usage or {}).get("total_tokens", 0) or 0)

        message = response.choices[0].message
        tool_calls = list(message.tool_calls or [])

        if not tool_calls:
            stop_reason = f"budget_{forced_reason}" if forced_reason else "no_tool_call"
            result_notes = str(message.content or "")
            steps.append(
                AgentStep(
                    step_index=len(steps),
                    tool_name="",
                    tool_args={},
                    observation_text=result_notes,
                    error=None,
                    elapsed_ms=turn_elapsed_ms,
                    tokens_used_delta=turn_tokens,
                    tokens_used_total=budget.tokens_used,
                )
            )
            break

        finish_call = next(
            (tc for tc in tool_calls if tc.function.name == FINISH_TOOL_NAME), None
        )
        if finish_call is not None:
            args = _safe_json_loads(finish_call.function.arguments)
            result_refs = _normalize_finish_refs(args.get("refs"))
            result_notes = str(args.get("notes") or "")
            stop_reason = f"budget_{forced_reason}" if forced_reason else "finished"
            steps.append(
                AgentStep(
                    step_index=len(steps),
                    tool_name=FINISH_TOOL_NAME,
                    tool_args=args,
                    observation_text=f"refs={len(result_refs)} notes={result_notes!r}",
                    error=None,
                    elapsed_ms=turn_elapsed_ms,
                    tokens_used_delta=turn_tokens,
                    tokens_used_total=budget.tokens_used,
                )
            )
            break

        if forced_reason is not None:
            # Forced tool_choice=finish but the provider returned a
            # different tool anyway (not observed in verification, but a
            # budget cutoff must never loop past). Stop here regardless.
            stop_reason = f"budget_{forced_reason}"
            result_notes = str(message.content or "") or (
                "budget exhausted; provider did not return finish"
            )
            steps.append(
                AgentStep(
                    step_index=len(steps),
                    tool_name="",
                    tool_args={},
                    observation_text=result_notes,
                    error="forced_finish_not_honored",
                    elapsed_ms=turn_elapsed_ms,
                    tokens_used_delta=turn_tokens,
                    tokens_used_total=budget.tokens_used,
                )
            )
            break

        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )
        first_tool_tokens_recorded = False
        for tc in tool_calls:
            tool_started = time.perf_counter()
            args = _safe_json_loads(tc.function.arguments)
            requested_name = str(tc.function.name or "")
            canonical_name = tool_name_map.get(requested_name, requested_name)
            try:
                tool_result = await REGISTRY.dispatch(canonical_name, tool_ctx, args)
            except Exception as exc:  # noqa: BLE001 - one broken tool must not kill the episode
                tool_result = ToolResult(text="", error=f"{type(exc).__name__}: {exc}")
            tool_elapsed_ms = int((time.perf_counter() - tool_started) * 1000)
            content = _tool_message_content(tool_result, max_chars=tool_ctx.budget.max_chars)
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": content}
            )
            # Turn-level token usage is attributed to the first tool step in
            # this turn (the completion that decided all calls in it); the
            # rest are 0 to avoid double-counting the same LLM usage.
            steps.append(
                AgentStep(
                    step_index=len(steps),
                    tool_name=canonical_name,
                    tool_args=args,
                    observation_text=content,
                    error=tool_result.error,
                    elapsed_ms=tool_elapsed_ms if first_tool_tokens_recorded else turn_elapsed_ms + tool_elapsed_ms,
                    tokens_used_delta=0 if first_tool_tokens_recorded else turn_tokens,
                    tokens_used_total=budget.tokens_used,
                )
            )
            first_tool_tokens_recorded = True

    return EpisodeResult(
        refs=result_refs,
        notes=result_notes,
        steps=steps,
        stop_reason=stop_reason,
        tokens_used=budget.tokens_used,
        model_name=model,
    )
