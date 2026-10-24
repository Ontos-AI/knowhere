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

When evidence review is enabled, the original deadline also covers SDK setup,
tool waits and the independent finish review; judge usage is added to the same
episode budget. Review freezes callbacks, and an accepted finish closes them.
A requested correction resumes in a new round using the existing read/pick
gates and request-scoped revision context.
"""

from __future__ import annotations

from shared.services.retrieval.document_scope import DocumentScope

import asyncio
import contextlib
import contextvars
import json
import os
import threading
import time
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from shared.services.retrieval.agent_explore.evidence_review import EvidenceReviewSession

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
_REVIEW_PENDING = "Evidence review is in progress; wait for the finish result."
_EPISODE_CLOSED = "The episode has ended; no further corpus tools are accepted."
_FINISH_ROUND_CLOSED = "Finish requested a continuation; start a new turn before retrieving."


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
        evidence_review: EvidenceReviewSession | None = None,
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
        current_round: dict[str, Any] = {
            "id": None, "pick_phase": False, "step_recorded": False,
        }
        request_context = contextvars.copy_context()
        review_state: dict[str, Any] = {
            "pending": False, "closed": False, "continued_round": None, "in_flight": 0,
            "stop_recorded": None,
        }
        pending_futures: set[Future[Any]] = set()

        steps: list[AgentStep] = []
        stop_reason = "finished"

        def _review_rejection(*, check_round: bool = True) -> str | None:
            if evidence_review is None:
                return None
            if review_state["closed"]:
                return _EPISODE_CLOSED
            if review_state["pending"]:
                return _REVIEW_PENDING
            if (
                check_round
                and review_state["continued_round"] is not None
                and review_state["continued_round"] == current_round["id"]
            ):
                return _FINISH_ROUND_CLOSED
            return None

        def _submit(work: Any) -> Future[Any]:
            if evidence_review is None:
                return asyncio.run_coroutine_threadsafe(work, loop)
            with state:
                if review_state["closed"]:
                    work.close()
                    future: Future[Any] = Future()
                    future.cancel()
                    return future
                future = request_context.copy().run(asyncio.run_coroutine_threadsafe, work, loop)
                pending_futures.add(future)
            return future

        def _close_review_episode(reason: str) -> None:
            if evidence_review is None:
                return
            with state:
                review_state["closed"] = True
                if reason in {"budget_wall_clock", "cancelled", "review_error"}:
                    budget.usage_complete = False
                if review_state["stop_recorded"] != reason:
                    evidence_review.on_stop(reason)
                    review_state["stop_recorded"] = reason
                for future in tuple(pending_futures):
                    future.cancel()
                state.notify_all()

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
                lambda: call_id in round_of or (
                    evidence_review is not None and review_state["closed"]
                ),
                timeout=max(0, budget.remaining_seconds()),
            ):
                return None, False
            if call_id not in round_of:
                return None, False
            model_call_id = round_of[call_id]
            if model_call_id != current_round["id"]:
                pool.begin_round()
                current_round["id"] = model_call_id
                current_round["pick_phase"] = pool.pick_phase
                current_round["step_recorded"] = False
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
                rejected = _review_rejection(check_round=False)
                if rejected is not None:
                    _reject(tool_name, args, rejected)
                    return rejected
                pick_round, new_round = _enter_round(ctx)
                round_index = pool.round_index
                rejected = _review_rejection()
                if rejected is not None:
                    _reject(tool_name, args, rejected)
                    return rejected
                if pick_round is None:
                    _reject(tool_name, args, _ROUND_TAG_MISSING)
                    return f"error: {_ROUND_TAG_MISSING}"
                if pick_round:
                    _reject(tool_name, args, _PICK_PHASE_REJECTION)
                    return _PICK_PHASE_REJECTION
                if new_round or (
                    evidence_review is not None and not current_round["step_recorded"]
                ):
                    exhausted = budget.exhausted() if evidence_review is not None else (
                        "max_steps" if budget.steps_used >= budget.max_steps else None
                    )
                    if exhausted is not None:
                        observation = (
                            (
                                _BUDGET_EXHAUSTED_MESSAGE if exhausted == "max_steps"
                                else "error: wall-clock budget exhausted for this episode; call finish now."
                            )
                            + "\n" + _state_tail(pool, budget)
                        )
                        _append_step(
                            tool_name=tool_name,
                            tool_args=args,
                            observation_text=observation,
                            error=f"budget_{exhausted}",
                            elapsed_ms=0,
                            round_index=round_index,
                        )
                        return observation
                    budget.record_step()
                    current_round["step_recorded"] = True
                readable = pool.readable()
                decided = pool.decided()
                if evidence_review is not None:
                    review_state["in_flight"] += 1
            tool_started = time.perf_counter()
            future = _submit(
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
            )
            try:
                tool_result = future.result(
                    timeout=max(0, budget.remaining_seconds()) if evidence_review is not None else 180
                )
            except Exception as exc:  # noqa: BLE001 - one broken tool must not kill the episode
                if evidence_review is not None:
                    future.cancel()
                tool_result = ToolResult(text="", error=f"{type(exc).__name__}: {exc}")
            elapsed_ms = int((time.perf_counter() - tool_started) * 1000)
            content = tool_message_content(
                tool_result,
                tool_name=tool_name,
                max_chars=tool_budget.max_chars,
            )
            with state:
                if evidence_review is not None:
                    pending_futures.discard(future)
                    review_state["in_flight"] -= 1
                    if review_state["closed"]:
                        _reject(tool_name, args, _EPISODE_CLOSED)
                        return _EPISODE_CLOSED
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
                rejected = _review_rejection(check_round=False)
                if rejected is not None:
                    _reject(PICK_TOOL_NAME, args, rejected)
                    return rejected
                pick_round, _new_round = _enter_round(ctx)
                rejected = _review_rejection()
                if rejected is not None:
                    _reject(PICK_TOOL_NAME, args, rejected)
                    return rejected
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
            nonlocal stop_reason
            args = dict(args or {})
            with state:
                rejected = _review_rejection(check_round=False)
                if rejected is not None:
                    _reject(FINISH_TOOL_NAME, args, rejected)
                    return json.dumps({"status": "error", "error": rejected})
                pick_round, _new_round = _enter_round(ctx)
                rejected = _review_rejection()
                if rejected is not None:
                    _reject(FINISH_TOOL_NAME, args, rejected)
                    return json.dumps({"status": "error", "error": rejected})
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
                if evidence_review is not None and review_state["in_flight"]:
                    message = "Wait for in-flight corpus tools and the required pick phase before finish."
                    _reject(FINISH_TOOL_NAME, args, message)
                    return json.dumps({"status": "error", "error": message})
                notes = str(args.get("notes") or "")
                _append_step(
                    tool_name=FINISH_TOOL_NAME,
                    tool_args=args,
                    observation_text=f"pool={len(pool.entries)} notes={notes!r}",
                    error=None,
                    elapsed_ms=0,
                    round_index=pool.round_index,
                )
                if evidence_review is not None:
                    exhausted = budget.exhausted()
                    if exhausted is not None:
                        stop_reason = f"budget_{exhausted}"
                        review_state["closed"] = True
                        return json.dumps({"status": "finished", "stop_reason": stop_reason, "pool": len(pool.entries)})
                    review_state["pending"] = True
                    reviewed_pool = list(pool.entries)
                    queried_tables = dict(pool.queried_tables())
                    review_round = pool.round_index
            if evidence_review is not None:
                review_started = time.perf_counter()
                future = _submit(evidence_review.on_finish(
                    pool=reviewed_pool, queried_tables=queried_tables, budget=budget,
                ))
                try:
                    decision = future.result(timeout=max(0, budget.remaining_seconds()))
                except Exception:  # noqa: BLE001 - preserve selected evidence if review cannot complete
                    future.cancel()
                    with state:
                        stop_reason = "budget_wall_clock" if budget.remaining_seconds() <= 0 else "review_error"
                        review_state["pending"] = False
                        review_state["closed"] = True
                    return json.dumps({"status": "finished", "stop_reason": stop_reason, "pool": len(pool.entries)})
                finally:
                    with state:
                        pending_futures.discard(future)
                with state:
                    review_state["pending"] = False
                    if review_state["closed"]:
                        return json.dumps({"status": "finished", "stop_reason": stop_reason, "pool": len(pool.entries)})
                    _append_step(
                        tool_name="evidence_review", tool_args={},
                        observation_text=json.dumps(evidence_review.report, ensure_ascii=False),
                        error=None,
                        elapsed_ms=int((time.perf_counter() - review_started) * 1000),
                        round_index=review_round,
                    )
                    if decision.continue_retrieval:
                        review_state["continued_round"] = current_round["id"]
                        return json.dumps({"status": "continue", "instruction": decision.instruction, "pool": len(pool.entries)})
                    review_state["closed"] = True
            return json.dumps({"status": "finished", "pool": len(pool.entries)})

        custom_tools[FINISH_TOOL_NAME] = cursor_sdk.CustomTool(
            execute=finish_execute,
            description=FINISH_TOOL_DESCRIPTION,
            input_schema=FINISH_TOOL_SCHEMA,
        )

        user_prompt = f"{AGENT_SYSTEM_PROMPT}\n\n---\n\nUser query:\n{query}"

        result: Any = None
        try:
            async with asyncio.timeout(
                max(0, budget.remaining_seconds()) if evidence_review is not None else None
            ) as lifecycle_deadline:
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
                                run.wait(), timeout=(
                                    max(0, budget.remaining_seconds())
                                    if evidence_review is not None else budget.wall_clock_seconds
                                )
                            )
                        except asyncio.TimeoutError:
                            stop_reason = "budget_wall_clock"
                            _close_review_episode(stop_reason)
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(run.cancel(), timeout=0.1 if evidence_review is not None else 10)
                            with contextlib.suppress(Exception):
                                result = await asyncio.wait_for(
                                    run.wait(), timeout=0.1 if evidence_review is not None else _CANCEL_GRACE_SECONDS
                                )
                        except asyncio.CancelledError:
                            _close_review_episode(
                                "budget_wall_clock" if lifecycle_deadline.expired() else "cancelled"
                            )
                            if evidence_review is not None:
                                with contextlib.suppress(Exception):
                                    await asyncio.wait_for(run.cancel(), timeout=0.1)
                            raise
        except asyncio.TimeoutError:
            if evidence_review is None:
                raise
            stop_reason = "budget_wall_clock"
            _close_review_episode(stop_reason)
        except asyncio.CancelledError:
            _close_review_episode("cancelled")
            raise

        if evidence_review is not None:
            if stop_reason == "finished" and not review_state["closed"]:
                stop_reason = "no_tool_call"
            _close_review_episode(stop_reason)

        usage = getattr(result, "usage", None)
        budget.record_usage(
            {"total_tokens": usage.total_tokens} if usage is not None else None
        )

        return EpisodeResult(
            pool=list(pool.entries),
            steps=steps,
            stop_reason=stop_reason,
            tokens_used=budget.tokens_used,
            model_name=self._model,
            queried_tables=dict(pool.queried_tables()),
        )
