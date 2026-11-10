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
from shared.services.retrieval.required_query_embedding import RequiredQueryEmbeddingException

import asyncio
import contextlib
import json
import os
import threading
import time
from collections.abc import Mapping
from concurrent.futures import Future
from contextvars import Context, copy_context
from typing import Any

from shared.services.ai.retrieval_ledger import record_retrieval_call
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
from shared.services.retrieval.agent_explore.cursor_retrieval_exception import (
    CursorRetrievalException,
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
from shared.services.retrieval.agent_tools import REGISTRY, Decision, ReadableAddresses, ToolBudget, ToolResult
from shared.services.retrieval.execution.native_failure import NativeFailureState
from shared.services.retrieval.lexical.error import LexicalIndexError

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
        required_failures: list[RequiredQueryEmbeddingException] = []
        failure_state: NativeFailureState = NativeFailureState.current() or NativeFailureState()
        fatal_signal: asyncio.Event = asyncio.Event()
        callback_context: Context = copy_context()
        pending_calls: dict[Future[ToolResult], bool] = {}
        pool = EvidencePool()
        round_of: dict[str, str] = {}
        current_round: dict[str, Any] = {"id": None, "pick_phase": False}

        steps: list[AgentStep] = []
        stop_reason = "finished"

        def _raise_fatal_failure() -> None:
            failure_state.raiseIfFailed()
            with state:
                if required_failures:
                    raise required_failures[0]

        def _record_required_failure(error: RequiredQueryEmbeddingException) -> None:
            with state:
                required_failures.append(error)
                state.notify_all()
            loop.call_soon_threadsafe(fatal_signal.set)

        def _on_delta(update: Any) -> None:
            if getattr(update, "type", None) != "tool-call-started":
                return
            with state:
                if failure_state.hasFailed or required_failures:
                    return
                round_of[update.call_id] = update.model_call_id
                state.notify_all()

        def _enter_round(ctx: Any) -> tuple[bool | None, bool]:
            """Wait for this call's round tag (caller holds ``state``).

            Returns ``(pick_phase, is_new_round)``. ``pick_phase`` is
            ``None`` when the tag never arrived.
            """
            _raise_fatal_failure()
            call_id = ctx.tool_call_id
            if not state.wait_for(
                lambda: call_id in round_of or failure_state.hasFailed or required_failures,
                timeout=max(0, budget.remaining_seconds()),
            ):
                return None, False
            _raise_fatal_failure()
            model_call_id = round_of[call_id]
            if model_call_id != current_round["id"]:
                pool.begin_round()
                current_round["id"] = model_call_id
                current_round["pick_phase"] = pool.pick_phase
                return current_round["pick_phase"], True
            return current_round["pick_phase"], False

        def _append_step(**fields: Any) -> None:
            _raise_fatal_failure()
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

        async def _dispatch_bound(
            tool_name: str,
            args: dict[str, Any],
            readable: ReadableAddresses,
            decided: Mapping[tuple[str, str], Decision],
            lit_pages_by_chunk: Mapping[tuple[str, str], tuple[int, ...]],
            lit_pages_by_path: Mapping[tuple[str, str], tuple[int, ...]],
        ) -> ToolResult:
            with failure_state.bind():
                _raise_fatal_failure()
                return await dispatch_tool_call(
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
                    lit_pages_by_chunk=lit_pages_by_chunk,
                    lit_pages_by_path=lit_pages_by_path,
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
                tool_started: float = time.perf_counter()
                is_recall: bool = tool_name == "corpus.recall"
                # SDK worker threads do not inherit request ContextVars.
                future: Future[ToolResult] = callback_context.copy().run(
                    asyncio.run_coroutine_threadsafe,
                    _dispatch_bound(
                        tool_name,
                        args,
                        pool.readable(),
                        pool.decided(),
                        pool.lit_pages_by_chunk(),
                        pool.lit_pages_by_path(),
                    ),
                    loop,
                )
                pending_calls[future] = is_recall
            try:
                tool_result: ToolResult = future.result(timeout=180)
                _raise_fatal_failure()
            except TimeoutError as exc:
                future.cancel()
                if is_recall:
                    _record_required_failure(RequiredQueryEmbeddingException(
                        internal_message="Required retrieval recall timed out",
                        provider="retrieval",
                        original_exception=exc,
                    ))
                    return "error: retrieval tool timed out"
                tool_result = ToolResult(text="", error="retrieval tool timed out")
            except RequiredQueryEmbeddingException as exc:
                # The SDK can turn callback exceptions into tool observations.
                # Retain the failure outside its loop and reject the episode.
                _record_required_failure(exc)
                return "error: required retrieval service failed"
            except LexicalIndexError as error:
                with state:
                    failure_state.record(error)
                    for pending in pending_calls:
                        if pending is not future:
                            pending.cancel()
                    state.notify_all()
                loop.call_soon_threadsafe(fatal_signal.set)
                raise
            except Exception as exc:  # noqa: BLE001 - one broken tool must not kill the episode
                _raise_fatal_failure()
                tool_result = ToolResult(text="", error=f"{type(exc).__name__}: {exc}")
            finally:
                with state:
                    pending_calls.pop(future, None)
            elapsed_ms = int((time.perf_counter() - tool_started) * 1000)
            content = tool_message_content(
                tool_result,
                tool_name=tool_name,
                max_chars=tool_budget.max_chars,
            )
            with state:
                _raise_fatal_failure()
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
        episode_started = time.monotonic()
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
                run_task: asyncio.Task[object] = asyncio.create_task(run.wait())
                fatal_task: asyncio.Task[bool] = asyncio.create_task(fatal_signal.wait())
                try:
                    done, _pending = await asyncio.wait(
                        {run_task, fatal_task}, timeout=budget.wall_clock_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    _raise_fatal_failure()
                    if run_task in done:
                        result = run_task.result()
                    else:
                        stop_reason = "budget_wall_clock"
                        with state:
                            if any(pending_calls.values()):
                                _record_required_failure(RequiredQueryEmbeddingException(
                                    internal_message="Required retrieval recall exceeded the episode deadline",
                                    provider="retrieval",
                                ))
                                for pending_call, is_recall in pending_calls.items():
                                    if is_recall:
                                        pending_call.cancel()
                        _raise_fatal_failure()
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(run.cancel(), timeout=10)
                        with contextlib.suppress(Exception):
                            result = await asyncio.wait_for(
                                asyncio.shield(run_task), timeout=_CANCEL_GRACE_SECONDS
                            )
                    _raise_fatal_failure()
                except (LexicalIndexError, RequiredQueryEmbeddingException):
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(run.cancel(), timeout=10)
                    raise
                finally:
                    with state:
                        for pending in pending_calls:
                            pending.cancel()
                        state.notify_all()
                    fatal_task.cancel()
                    if not run_task.done():
                        run_task.cancel()
                    await asyncio.gather(run_task, fatal_task, return_exceptions=True)

        _raise_fatal_failure()

        if result is not None:
            status = result.status
            if status == "error" or (
                status == "cancelled" and stop_reason != "budget_wall_clock"
            ):
                failure = CursorRetrievalException(
                    model=self._model,
                    status=status,
                    provider_message=getattr(result, "error_message", None),
                    provider_code=getattr(result, "error_code", None),
                    run_id=result.id,
                    request_id=getattr(result, "request_id", None)
                    or getattr(run, "request_id", None),
                )
                raise failure

        duration_ms = max(int((time.monotonic() - episode_started) * 1000), 0)
        usage: dict[str, int] | None = None
        if result is not None and getattr(result, "usage", None) is not None:
            try:
                total_tokens = int(result.usage.total_tokens)
            except (TypeError, ValueError):
                total_tokens = None
            if total_tokens is not None:
                usage = {"total_tokens": total_tokens}
                budget.record_usage(usage)
        record_retrieval_call(
            usage,
            model=self._model,
            task="retrieval.agent_explore",
            duration_ms=duration_ms,
        )

        return EpisodeResult(
            pool=list(pool.entries),
            steps=steps,
            stop_reason=stop_reason,
            tokens_used=budget.tokens_used,
            model_name=self._model,
            queried_tables=dict(pool.queried_tables()),
        )
