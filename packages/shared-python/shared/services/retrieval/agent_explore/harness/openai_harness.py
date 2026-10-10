"""In-process tool-calling episode over the ``agent_tools`` corpus registry,
via an OpenAI-compatible (DeepSeek) function-calling loop.

Uses ``OpenAICompatibleClientSync.chat_completion_raw_with_usage`` — the RAW
response, not ``chat_completion_with_usage`` — because the latter only
returns ``.content`` and silently drops ``message.tool_calls``. Verified live
(2026-09-08) against ``deepseek-v4-flash`` via this codebase's client:
single tool call, parallel tool calls in one turn, tool-result feedback +
final synthesis, and forced ``tool_choice`` (used for the budget-exhaustion
cutoff and the pick phase below) all work.

Each turn is one round and sends only a system message plus one rebuilt
user message (query, one-line call trace, latest-turn results with pick ids,
evidence pool, steps). Native tool-call history is not kept.

A turn after a successful read or outline is a pick phase: only
``corpus_pick`` is offered and forced, and the turn does not count as a
step. Tokens are only summed for the episode total.

Tool calls within one turn are dispatched sequentially through
``dispatch.dispatch_tool_call`` (fresh DB session per call). No import from
archived map-nav modules.

An optional evidence-review session intercepts a valid natural finish. Its
feedback is carried separately from the original query, and correction keeps
the same evidence pool and read/pick gates. Enabled episodes bound every await
by the remaining wall clock and send that timeout to the provider; cancelling
the await cannot forcibly interrupt an already-running synchronous SDK request.
"""

from __future__ import annotations

from shared.services.retrieval.document_scope import DocumentScope

import asyncio
import json
import time
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_explore.config import (
    AGENT_EXPLORE_MAX_COMPLETION_TOKENS,
    AGENT_EXPLORE_MODEL,
    FINISH_TOOL_DESCRIPTION,
    FINISH_TOOL_NAME,
    FINISH_TOOL_SCHEMA,
    PICK_TOOL_DESCRIPTION,
    PICK_TOOL_NAME,
    PICK_TOOL_SCHEMA,
)
from shared.services.retrieval.agent_explore.dispatch import DbFactory, dispatch_tool_call
from shared.services.retrieval.agent_explore.evidence_pool import (
    EvidencePool,
    render_budget,
    render_pick_outcome,
    render_trace_line,
    trace_status,
)
from shared.services.retrieval.agent_explore.prompt import AGENT_SYSTEM_PROMPT
from shared.services.retrieval.agent_explore.shared import (
    build_wire_tool_name_map,
    invalid_finish_message,
    invalid_pick_message,
    read_ref_status,
    tool_message_content,
    validate_finish_args,
    validate_pick_args,
    wire_safe_tool_name,
)
from shared.services.retrieval.agent_explore.types import AgentStep, EpisodeResult
from shared.services.retrieval.agent_tools import REGISTRY, ToolBudget, ToolResult

if TYPE_CHECKING:
    from shared.services.retrieval.agent_explore.evidence_review import EvidenceReviewSession

_PICK_WIRE_NAME = wire_safe_tool_name(PICK_TOOL_NAME)

_PICK_PHASE_INSTRUCTION = (
    "Pick phase: you can only call corpus_pick now. Pick the ids worth "
    "keeping, or pass an empty list."
)


def _resolve_client_and_model(*, max_retries: int | None = None) -> tuple[Any, str]:
    """Resolve the OpenAI-compatible client and ``AGENT_EXPLORE_MODEL``."""
    from shared.services.ai.llm_overrides import resolve_text
    from shared.services.ai.openai_compatible_client_sync import get_openai_client

    requested = AGENT_EXPLORE_MODEL
    effective_model, api_key, api_url = resolve_text(requested)
    model = effective_model or requested
    options = {} if max_retries is None else {"max_retries": max_retries}
    client = get_openai_client(model=model, api_key=api_key, api_url=api_url, **options)
    return client, model


def _build_openai_tools() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return ``(tools, name_map)`` where ``name_map`` maps the wire-safe
    name back to the canonical ``REGISTRY`` name (``finish`` maps to itself).
    """
    specs = REGISTRY.all()
    name_map = build_wire_tool_name_map([spec.name for spec in specs])
    name_map[FINISH_TOOL_NAME] = FINISH_TOOL_NAME
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": wire_safe_tool_name(spec.name),
                "description": spec.description,
                "parameters": spec.json_schema,
            },
        }
        for spec in specs
    ]
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


def _build_pick_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _PICK_WIRE_NAME,
            "description": PICK_TOOL_DESCRIPTION,
            "parameters": PICK_TOOL_SCHEMA,
        },
    }


def _parse_tool_arguments(raw: str | None) -> tuple[dict[str, Any], str | None]:
    """Parse one tool call's JSON arguments, or return the parse error.

    Only a missing argument string or an explicit ``{}`` is an empty object;
    an empty string or malformed JSON is an error. Either way the error
    fails just that one call (a corpus.* tool or ``finish``) and is sent
    back to the model — the episode continues.
    """
    if raw is None:
        return {}, None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return {}, f"invalid JSON arguments: {exc}"
    if not isinstance(parsed, dict):
        return {}, f"arguments must be a JSON object, got {type(parsed).__name__}"
    return parsed, None


def _compose_user_content(
    *,
    query: str,
    trace_lines: list[str],
    latest_results: list[str],
    latest_turn: int,
    pool: EvidencePool,
    budget: EpisodeBudget,
    instruction: str,
) -> str:
    parts = [f"User query: {query}"]
    if trace_lines:
        parts.append("trace:\n" + "\n".join(f"  {line}" for line in trace_lines))
    if latest_results:
        parts.append(
            f"latest results (turn {latest_turn}):\n" + "\n\n".join(latest_results)
        )
    parts.append(pool.render_pool())
    parts.append(render_budget(budget))
    parts.append(instruction)
    return "\n\n".join(part for part in parts if part)


class OpenAIHarness:
    """``Harness`` implementation over an OpenAI-compatible function-calling loop."""

    async def run_episode(
        self,
        *,
        db_factory: DbFactory,
        user_id: str,
        namespace: str,
        document_scope: DocumentScope = DocumentScope(),
        query: str,
        budget: EpisodeBudget,
        evidence_review: EvidenceReviewSession | None = None,
    ) -> EpisodeResult:
        tool_budget = ToolBudget()
        client, model = (
            _resolve_client_and_model(max_retries=0)
            if evidence_review is not None else _resolve_client_and_model()
        )
        openai_tools, tool_name_map = _build_openai_tools()
        pick_tool = _build_pick_tool()
        pool = EvidencePool()
        trace_lines: list[str] = []
        latest_results: list[str] = []
        latest_turn = 0
        steps: list[AgentStep] = []
        result_notes = ""
        turn_index = 0
        stop_reason = "finished"
        review_feedback = ""

        async def _within_deadline(work: Awaitable[Any], *, usage_in_flight: bool = False) -> Any:
            try:
                return await asyncio.wait_for(work, timeout=max(0, budget.remaining_seconds()))
            except asyncio.TimeoutError:
                if usage_in_flight:
                    budget.usage_complete = False
                raise
            except asyncio.CancelledError:
                if usage_in_flight:
                    budget.usage_complete = False
                if evidence_review is not None:
                    evidence_review.on_stop("cancelled")
                raise

        while True:
            if evidence_review is not None and budget.remaining_seconds() <= 0:
                stop_reason = "budget_wall_clock"
                break
            turn_index += 1
            pool.begin_round()
            pick_turn = pool.pick_phase
            forced_reason = None if pick_turn else budget.exhausted()
            if evidence_review is not None and forced_reason is not None:
                stop_reason = f"budget_{forced_reason}"
                break
            if pick_turn:
                tools = [pick_tool]
                tool_choice: Any = {"type": "function", "function": {"name": _PICK_WIRE_NAME}}
            else:
                tools = openai_tools
                tool_choice = "auto"
                if forced_reason is not None:
                    tool_choice = {"type": "function", "function": {"name": FINISH_TOOL_NAME}}
            messages = [
                {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _compose_user_content(
                        query=query,
                        trace_lines=trace_lines,
                        latest_results=latest_results,
                        latest_turn=latest_turn,
                        pool=pool,
                        budget=budget,
                        instruction="\n\n".join(
                            part for part in (
                                review_feedback,
                                _PICK_PHASE_INSTRUCTION if pick_turn else "",
                            ) if part
                        ),
                    ),
                },
            ]

            turn_started = time.perf_counter()
            request_options = (
                {"timeout": max(0.001, budget.remaining_seconds())}
                if evidence_review is not None else {}
            )
            completion = asyncio.to_thread(
                client.chat_completion_raw_with_usage,
                messages=messages,
                model=model,
                temperature=0.0,
                max_tokens=AGENT_EXPLORE_MAX_COMPLETION_TOKENS,
                tools=tools,
                tool_choice=tool_choice,
                **request_options,
            )
            try:
                response, usage = (
                    await _within_deadline(completion, usage_in_flight=True)
                    if evidence_review is not None
                    else await completion
                )
            except asyncio.TimeoutError:
                if evidence_review is None:
                    raise
                stop_reason = "budget_wall_clock"
                break
            budget.record_usage(usage)
            if not pick_turn:
                budget.record_step()
            turn_elapsed_ms = int((time.perf_counter() - turn_started) * 1000)

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
                        round_index=pool.round_index,
                    )
                )
                break

            if pick_turn:
                for call_index, tool_call in enumerate(tool_calls):
                    args, parse_error = _parse_tool_arguments(tool_call.function.arguments)
                    pick_error = parse_error or validate_pick_args(args)
                    if pick_error is not None:
                        message = invalid_pick_message(pick_error)
                        trace_lines.append(
                            render_trace_line(
                                len(trace_lines) + 1,
                                _PICK_WIRE_NAME,
                                args,
                                trace_status(ToolResult(text="", error=message)),
                            )
                        )
                        steps.append(
                            AgentStep(
                                step_index=len(steps),
                                tool_name=PICK_TOOL_NAME,
                                tool_args=args,
                                observation_text=message,
                                error=message,
                                elapsed_ms=turn_elapsed_ms if call_index == 0 else 0,
                                round_index=pool.round_index,
                            )
                        )
                        continue
                    outcome = pool.apply_pick(list(args["pick"]))
                    observation = render_pick_outcome(outcome)
                    trace_lines.append(
                        render_trace_line(
                            len(trace_lines) + 1, _PICK_WIRE_NAME, args, observation
                        )
                    )
                    steps.append(
                        AgentStep(
                            step_index=len(steps),
                            tool_name=PICK_TOOL_NAME,
                            tool_args=args,
                            observation_text=observation,
                            error=None,
                            elapsed_ms=turn_elapsed_ms if call_index == 0 else 0,
                            round_index=pool.round_index,
                            picked=outcome.added,
                            pick_rejected=outcome.rejected,
                        )
                    )
                continue

            parsed_calls: list[tuple[str, dict[str, Any], str | None]] = [
                (
                    str(tool_call.function.name or ""),
                    *_parse_tool_arguments(tool_call.function.arguments),
                )
                for tool_call in tool_calls
            ]

            finish_entry = next(
                (item for item in parsed_calls if item[0] == FINISH_TOOL_NAME),
                None,
            )
            finish_error: str | None = None
            finish_args: dict[str, Any] = {}
            finish_parse_error: str | None = None
            if finish_entry is not None:
                _name, finish_args, finish_parse_error = finish_entry
                parse_error = finish_parse_error
                if parse_error is None:
                    parse_error = validate_finish_args(finish_args)
                if parse_error is not None and forced_reason is None:
                    finish_error = parse_error
            if finish_entry is not None and finish_error is None:
                result_notes = (
                    finish_parse_error
                    if finish_parse_error is not None
                    else str(finish_args.get("notes") or "")
                )
                stop_reason = f"budget_{forced_reason}" if forced_reason else "finished"
                steps.append(
                    AgentStep(
                        step_index=len(steps),
                        tool_name=FINISH_TOOL_NAME,
                        tool_args=finish_args,
                        observation_text=f"pool={len(pool.entries)} notes={result_notes!r}",
                        error=finish_parse_error,
                        elapsed_ms=turn_elapsed_ms,
                        round_index=pool.round_index,
                    )
                )
                if evidence_review is not None:
                    exhausted = budget.exhausted()
                    if exhausted is not None:
                        stop_reason = f"budget_{exhausted}"
                        break
                    review_started = time.perf_counter()
                    try:
                        decision = await _within_deadline(
                            evidence_review.on_finish(
                                pool=list(pool.entries),
                                queried_tables=dict(pool.queried_tables()),
                                budget=budget,
                            ),
                            usage_in_flight=True,
                        )
                    except asyncio.TimeoutError:
                        stop_reason = "budget_wall_clock"
                        break
                    steps.append(
                        AgentStep(
                            step_index=len(steps),
                            tool_name="evidence_review",
                            tool_args={},
                            observation_text=json.dumps(evidence_review.report, ensure_ascii=False),
                            error=None,
                            elapsed_ms=int((time.perf_counter() - review_started) * 1000),
                            round_index=pool.round_index,
                        )
                    )
                    if decision.continue_retrieval:
                        review_feedback = decision.instruction
                        continue
                break

            if forced_reason is not None:
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
                        round_index=pool.round_index,
                    )
                )
                break

            readable = pool.readable()
            decided = pool.decided()
            first_step_recorded = False
            turn_results: list[str] = []
            tool_timed_out = False
            for requested_name, call_args, parse_error in parsed_calls:
                tool_started = time.perf_counter()
                canonical_name = tool_name_map.get(requested_name, requested_name)
                if requested_name == FINISH_TOOL_NAME:
                    tool_result = ToolResult(
                        text="", error=invalid_finish_message(str(finish_error))
                    )
                    trace_result = ToolResult(text="", error=str(finish_error))
                elif parse_error is not None:
                    tool_result = ToolResult(text="", error=parse_error)
                    trace_result = tool_result
                else:
                    dispatch = dispatch_tool_call(
                        canonical_name,
                        call_args,
                        db_factory=db_factory,
                        user_id=user_id,
                        namespace=namespace,
                        document_scope=document_scope,
                        budget=tool_budget,
                        query=query,
                        readable=readable,
                        decided=decided,
                    )
                    try:
                        tool_result = (
                            await _within_deadline(dispatch)
                            if evidence_review is not None
                            else await dispatch
                        )
                    except asyncio.TimeoutError:
                        if evidence_review is None:
                            raise
                        tool_timed_out = True
                        break
                    trace_result = tool_result
                tool_elapsed_ms = int((time.perf_counter() - tool_started) * 1000)
                content = tool_message_content(
                    tool_result,
                    tool_name=canonical_name,
                    max_chars=tool_budget.max_chars,
                )
                candidates = pool.issue(canonical_name, tool_result)
                if candidates:
                    content = f"{content}\n{pool.render_candidates(candidates)}"
                turn_results.append(f"{requested_name}:\n{content}")
                trace_lines.append(
                    render_trace_line(
                        len(trace_lines) + 1,
                        requested_name,
                        call_args,
                        trace_status(trace_result),
                    )
                )
                steps.append(
                    AgentStep(
                        step_index=len(steps),
                        tool_name=canonical_name,
                        tool_args=call_args,
                        observation_text=content,
                        error=tool_result.error,
                        elapsed_ms=(
                            tool_elapsed_ms
                            if first_step_recorded
                            else turn_elapsed_ms + tool_elapsed_ms
                        ),
                        round_index=pool.round_index,
                        ref_status=read_ref_status(canonical_name, tool_result),
                        candidates=[item.handle for item in candidates] or None,
                    )
                )
                first_step_recorded = True

            if tool_timed_out:
                stop_reason = (
                    "budget_wall_clock" if budget.remaining_seconds() <= 0 else "tool_timeout"
                )
                break
            latest_results = turn_results
            latest_turn = turn_index

        if evidence_review is not None:
            evidence_review.on_stop(stop_reason)
        return EpisodeResult(
            pool=list(pool.entries),
            steps=steps,
            stop_reason=stop_reason,
            tokens_used=budget.tokens_used,
            model_name=model,
            queried_tables=dict(pool.queried_tables()),
        )
