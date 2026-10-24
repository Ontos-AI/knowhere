"""``Harness`` implementation over the Cursor SDK local agent.

Promoted from ``apps/worker/scripts/debug_cursor_agent_explore.py`` (PoC) —
see Phase 3.5 of
``.cursor/plans/agentic_corpus_explore_retrieval_c2c4ea21.plan.md``. Uses
``cursor_sdk``'s ``local.custom_tools`` so this host process still executes
``agent_tools.REGISTRY`` (same ``corpus.*`` tools as ``harness/openai_harness.py``),
while the orchestration model comes from Cursor (default
``config.AGENT_EXPLORE_CURSOR_MODEL``, e.g. ``composer-2.5``) instead of
DeepSeek.

Requires ``cursor-sdk`` (base dependency of ``apps/api``; worker debug
scripts use the ``cursor-harness`` extra) and ``CURSOR_API_KEY``. The
guarded import in ``_require_cursor_sdk`` raises a clear error at episode
start if ``cursor-sdk`` isn't installed.

This harness does not control the LLM turn loop. ``agent.send(...)`` +
``await run.wait()`` hands the multi-turn tool-calling loop to the Cursor
SDK. The host process only sees ``execute`` callbacks, ``on_delta`` updates
(driven by ``run.wait()``), and the terminal ``RunResult``.

Rounds: calls the model issues in one turn share one ``model_call_id``,
carried by the ``tool-call-started`` update. That update and the tool
callback arrive almost together, so each callback first waits for its own
tag (at most the remaining wall clock; without it the call fails with
``round tag missing``, unexecuted and uncounted). A new ``model_call_id``
begins a new pool round.

Pick phase: the Cursor tool list is fixed when the agent is created, so a
round after a successful read or outline accepts only ``corpus_pick``; every
other call (including ``finish``) is rejected at once, unexecuted and
uncounted. ``corpus_pick`` itself never counts as a step.

Budget dimensions:

- **``max_steps``**: one explore round (shared ``model_call_id``) is one
  step, matching the OpenAI harness. Checked when a new explore round
  starts. Once ``budget.steps_used`` reaches ``budget.max_steps``, further
  explore rounds are not forwarded; the callback returns a fixed exhausted
  message plus pool and budget. Same-round sibling ``corpus.*`` calls share
  that one step and still dispatch.
- **``wall_clock``**: ``asyncio.wait_for(run.wait(), timeout=budget.wall_clock_seconds)``.
  On timeout, best-effort ``await run.cancel()``.
- **tokens**: not limited; ``budget.tokens_used`` is only the episode total
  from the terminal ``RunResult.usage``.
"""

from __future__ import annotations

from shared.services.retrieval.document_scope import DocumentScope

import asyncio
import contextlib
import json
import os
import threading
import time
from typing import Any

from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_explore.config import (
    AGENT_EXPLORE_CURSOR_MODEL,
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
)
from shared.services.retrieval.agent_explore.prompt import AGENT_SYSTEM_PROMPT
from shared.services.retrieval.agent_explore.shared import (
    cursor_execute_content,
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

# Grace period for the underlying agent run to actually stop, after a
# best-effort run.cancel() following a wall_clock timeout, before this
# process gives up waiting for a terminal RunResult.
_CANCEL_GRACE_SECONDS = 30.0

_BUDGET_EXHAUSTED_MESSAGE = (
    "error: step budget exhausted for this episode — do not call any more "
    "corpus.* tools; call finish now."
)

_PICK_PHASE_REJECTION = (
    "Pick phase: only corpus_pick is accepted now. Pick from R/O ids above, "
    "or pass an empty list."
)

_PICK_PHASE_INSTRUCTION = (
    "Pick phase: you can only call corpus_pick now. Pick the ids worth "
    "keeping, or pass an empty list."
)

_ROUND_TAG_MISSING = "round tag missing"


def _require_cursor_sdk() -> Any:
    try:
        import cursor_sdk
    except ImportError as exc:
        raise RuntimeError(
            "AGENT_EXPLORE_HARNESS=cursor_sdk requires the "
            "'cursor-sdk' dependency, which is not installed in this "
            "interpreter. For the API service it is a base dependency "
            "(apps/api). Worker debug scripts: uv sync --extra cursor-harness."
        ) from exc
    return cursor_sdk


def _state_tail(pool: EvidencePool, budget: EpisodeBudget) -> str:
    return "\n\n".join([pool.render_pool(), render_budget(budget)])


class CursorHarness:
    """``Harness`` implementation over the Cursor SDK local agent."""

    def __init__(self, *, model: str | None = None) -> None:
        env_model = os.environ.get("AGENT_EXPLORE_CURSOR_MODEL", "").strip()
        self._model = model or env_model or AGENT_EXPLORE_CURSOR_MODEL

    async def run_episode(
        self,
        *,
        db_factory: DbFactory,
        user_id: str,
        namespace: str,
        document_scope: DocumentScope = DocumentScope(),
        query: str,
        budget: EpisodeBudget,
    ) -> EpisodeResult:
        cursor_sdk = _require_cursor_sdk()

        api_key = os.environ.get("CURSOR_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "AGENT_EXPLORE_HARNESS=cursor_sdk requires CURSOR_API_KEY to be set"
            )

        tool_budget = ToolBudget()
        loop = asyncio.get_running_loop()
        state = threading.Condition()
        pool = EvidencePool()
        round_of: dict[str, str] = {}
        current_round: dict[str, Any] = {"id": None, "pick_phase": False}

        steps: list[AgentStep] = []
        stop_reason = "finished"

        def _on_delta(update: Any) -> None:
            if getattr(update, "type", None) != "tool-call-started":
                return
            with state:
                round_of[update.call_id] = update.model_call_id
                state.notify_all()

        def _enter_round(ctx: Any) -> tuple[bool | None, bool]:
            """Wait for this call's round tag (caller holds ``state``).

            Returns ``(pick_phase, is_new_round)``. ``pick_phase`` is
            ``None`` when the tag never arrived.
            """
            call_id = ctx.tool_call_id
            if not state.wait_for(
                lambda: call_id in round_of, timeout=budget.remaining_seconds()
            ):
                return None, False
            model_call_id = round_of[call_id]
            if model_call_id != current_round["id"]:
                pool.begin_round()
                current_round["id"] = model_call_id
                current_round["pick_phase"] = pool.pick_phase
                return current_round["pick_phase"], True
            return current_round["pick_phase"], False

        def _append_step(**fields: Any) -> None:
            steps.append(AgentStep(step_index=len(steps), **fields))

        def _reject(tool_name: str, args: dict[str, Any], message: str) -> None:
            _append_step(
                tool_name=tool_name,
                tool_args=args,
                observation_text=message,
                error=message,
                elapsed_ms=0,
                round_index=pool.round_index,
            )

        def _dispatch_sync(
            tool_name: str, args: dict[str, Any], ctx: Any
        ) -> str | list[dict[str, Any]]:
            with state:
                pick_round, new_round = _enter_round(ctx)
                round_index = pool.round_index
                if pick_round is None:
                    _reject(tool_name, args, _ROUND_TAG_MISSING)
                    return f"error: {_ROUND_TAG_MISSING}"
                if pick_round:
                    _reject(tool_name, args, _PICK_PHASE_REJECTION)
                    return _PICK_PHASE_REJECTION
                if new_round:
                    if budget.steps_used >= budget.max_steps:
                        observation = (
                            _BUDGET_EXHAUSTED_MESSAGE + "\n" + _state_tail(pool, budget)
                        )
                        _append_step(
                            tool_name=tool_name,
                            tool_args=args,
                            observation_text=observation,
                            error="budget_max_steps",
                            elapsed_ms=0,
                            round_index=round_index,
                        )
                        return observation
                    budget.record_step()
                readable = pool.readable()
                decided = pool.decided()
            tool_started = time.perf_counter()
            future = asyncio.run_coroutine_threadsafe(
                dispatch_tool_call(
                    tool_name,
                    args,
                    db_factory=db_factory,
                    user_id=user_id,
                    namespace=namespace,
                    document_scope=document_scope,
                    budget=tool_budget,
                    query=query,
                    readable=readable,
                    decided=decided,
                ),
                loop,
            )
            try:
                tool_result = future.result(timeout=180)
            except Exception as exc:  # noqa: BLE001 - one broken tool must not kill the episode
                tool_result = ToolResult(text="", error=f"{type(exc).__name__}: {exc}")
            elapsed_ms = int((time.perf_counter() - tool_started) * 1000)
            content = tool_message_content(
                tool_result,
                tool_name=tool_name,
                max_chars=tool_budget.max_chars,
            )
            with state:
                candidates = pool.issue(tool_name, tool_result)
                tail = _state_tail(pool, budget)
            text = content
            if candidates:
                text = (
                    f"{content}\n{pool.render_candidates(candidates)}\n"
                    f"{_PICK_PHASE_INSTRUCTION}"
                )
            text = text + "\n\n" + tail
            observation = cursor_execute_content(tool_result, text=text)
            with state:
                _append_step(
                    tool_name=tool_name,
                    tool_args=args,
                    observation_text=text,
                    error=tool_result.error,
                    elapsed_ms=elapsed_ms,
                    round_index=round_index,
                    ref_status=read_ref_status(tool_name, tool_result),
                    candidates=[item.handle for item in candidates] or None,
                )
            return observation

        custom_tools: dict[str, Any] = {}
        for spec in REGISTRY.all():
            wire_name = wire_safe_tool_name(spec.name)

            def _make_execute(resolved_name: str):
                def execute(
                    args: dict[str, Any], ctx: Any
                ) -> str | list[dict[str, Any]]:
                    return _dispatch_sync(resolved_name, dict(args or {}), ctx)

                return execute

            custom_tools[wire_name] = cursor_sdk.CustomTool(
                execute=_make_execute(spec.name),
                description=f"{spec.description} (canonical name: {spec.name})",
                input_schema=spec.json_schema,
            )

        def pick_execute(args: dict[str, Any], ctx: Any) -> str:
            args = dict(args or {})
            with state:
                pick_round, _new_round = _enter_round(ctx)
                if pick_round is None:
                    _reject(PICK_TOOL_NAME, args, _ROUND_TAG_MISSING)
                    return f"error: {_ROUND_TAG_MISSING}"
                validation_error = validate_pick_args(args)
                if validation_error is not None:
                    message = invalid_pick_message(validation_error)
                    _reject(PICK_TOOL_NAME, args, message)
                    return f"error: {message}"
                outcome = pool.apply_pick(list(args["pick"]))
                observation = render_pick_outcome(outcome)
                _append_step(
                    tool_name=PICK_TOOL_NAME,
                    tool_args=args,
                    observation_text=observation,
                    error=None,
                    elapsed_ms=0,
                    round_index=pool.round_index,
                    picked=outcome.added,
                    pick_rejected=outcome.rejected,
                )
                return observation + "\n\n" + _state_tail(pool, budget)

        custom_tools[wire_safe_tool_name(PICK_TOOL_NAME)] = cursor_sdk.CustomTool(
            execute=pick_execute,
            description=PICK_TOOL_DESCRIPTION,
            input_schema=PICK_TOOL_SCHEMA,
        )

        def finish_execute(args: dict[str, Any], ctx: Any) -> str:
            args = dict(args or {})
            with state:
                pick_round, _new_round = _enter_round(ctx)
                if pick_round is None or pick_round:
                    message = _ROUND_TAG_MISSING if pick_round is None else _PICK_PHASE_REJECTION
                    _reject(FINISH_TOOL_NAME, args, message)
                    return json.dumps({"status": "error", "error": message})
                validation_error = validate_finish_args(args)
                if validation_error is not None:
                    _reject(FINISH_TOOL_NAME, args, validation_error)
                    return json.dumps(
                        {
                            "status": "error",
                            "error": invalid_finish_message(validation_error),
                        }
                    )
                notes = str(args.get("notes") or "")
                _append_step(
                    tool_name=FINISH_TOOL_NAME,
                    tool_args=args,
                    observation_text=f"pool={len(pool.entries)} notes={notes!r}",
                    error=None,
                    elapsed_ms=0,
                    round_index=pool.round_index,
                )
            return json.dumps({"status": "finished", "pool": len(pool.entries)})

        custom_tools[FINISH_TOOL_NAME] = cursor_sdk.CustomTool(
            execute=finish_execute,
            description=FINISH_TOOL_DESCRIPTION,
            input_schema=FINISH_TOOL_SCHEMA,
        )

        user_prompt = f"{AGENT_SYSTEM_PROMPT}\n\n---\n\nUser query:\n{query}"

        result: Any = None
        async with await cursor_sdk.AsyncClient.launch_bridge(
            workspace=os.getcwd(),
        ) as client:
            async with await client.agents.create(
                cursor_sdk.AgentOptions(
                    api_key=api_key,
                    model=self._model,
                    local=cursor_sdk.LocalAgentOptions(
                        cwd=os.getcwd(),
                        custom_tools=custom_tools,
                    ),
                )
            ) as agent:
                run = await agent.send(
                    user_prompt, cursor_sdk.SendOptions(on_delta=_on_delta)
                )
                try:
                    result = await asyncio.wait_for(
                        run.wait(), timeout=budget.wall_clock_seconds
                    )
                except asyncio.TimeoutError:
                    stop_reason = "budget_wall_clock"
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(run.cancel(), timeout=10)
                    with contextlib.suppress(Exception):
                        result = await asyncio.wait_for(
                            run.wait(), timeout=_CANCEL_GRACE_SECONDS
                        )

        result_usage_total_tokens = 0
        if result is not None and result.usage is not None:
            result_usage_total_tokens = result.usage.total_tokens
        budget.record_usage({"total_tokens": result_usage_total_tokens})

        return EpisodeResult(
            pool=list(pool.entries),
            steps=steps,
            stop_reason=stop_reason,
            tokens_used=budget.tokens_used,
            model_name=self._model,
            queried_tables=dict(pool.queried_tables()),
        )
