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
SDK. The host process only sees ``execute`` callbacks and the terminal
``RunResult``. There is no per-turn hook to rebuild the decision context
the way ``openai_harness.py`` does; each callback returns pick ids for
this result, the evidence pool, and the remaining budget.

Budget dimensions:

- **``max_steps``**: checked in ``_dispatch_sync`` before dispatch. Once
  ``budget.steps_used`` reaches ``budget.max_steps``, further ``corpus.*``
  calls are not forwarded; the callback still applies ``pick`` and returns
  a fixed exhausted message plus pool and budget.
- **``wall_clock``**: ``asyncio.wait_for(run.wait(), timeout=budget.wall_clock_seconds)``.
  On timeout, best-effort ``await run.cancel()``.
- **``token_limit``**: not actively enforced mid-run; ``budget.tokens_used``
  is populated post-hoc from the terminal ``RunResult.usage``.
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
)
from shared.services.retrieval.agent_explore.dispatch import DbFactory, dispatch_tool_call
from shared.services.retrieval.agent_explore.evidence_pool import (
    EvidencePool,
    PickOutcome,
    render_budget,
)
from shared.services.retrieval.agent_explore.prompt import (
    AGENT_SYSTEM_PROMPT,
    split_pick,
    with_pick_field,
)
from shared.services.retrieval.agent_explore.shared import (
    cursor_execute_content,
    invalid_finish_message,
    read_ref_status,
    tool_message_content,
    validate_finish_args,
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


def _attach_pick(
    step: AgentStep, requested: list[str], outcome: PickOutcome
) -> AgentStep:
    step.pick_requested = requested
    step.picked = outcome.added
    step.pick_rejected = outcome.rejected
    return step


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
        budget_lock = threading.Lock()
        pool = EvidencePool()

        steps: list[AgentStep] = []
        finish_state: dict[str, Any] = {"notes": ""}
        stop_reason = "finished"

        def _dispatch_sync(
            tool_name: str, args: dict[str, Any]
        ) -> str | list[dict[str, Any]]:
            handles, call_args = split_pick(args)
            with budget_lock:
                outcome = pool.apply_pick(handles, pool.take_pending())
                exhausted = budget.steps_used >= budget.max_steps
                if not exhausted:
                    budget.record_step()
            if exhausted:
                observation = _BUDGET_EXHAUSTED_MESSAGE + "\n" + _state_tail(pool, budget)
                with budget_lock:
                    steps.append(
                        _attach_pick(
                            AgentStep(
                                step_index=len(steps),
                                tool_name=tool_name,
                                tool_args=call_args,
                                observation_text=observation,
                                error="budget_max_steps",
                                elapsed_ms=0,
                                tokens_used_delta=0,
                                tokens_used_total=budget.tokens_used,
                            ),
                            handles,
                            outcome,
                        )
                    )
                return observation
            tool_started = time.perf_counter()
            future = asyncio.run_coroutine_threadsafe(
                dispatch_tool_call(
                    tool_name,
                    call_args,
                    db_factory=db_factory,
                    user_id=user_id,
                    namespace=namespace,
                    document_scope=document_scope,
                    budget=tool_budget,
                    query=query,
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
            with budget_lock:
                candidates = pool.issue(tool_name, tool_result)
                tail = _state_tail(pool, budget)
            candidate_line = pool.render_candidates(candidates)
            text = content
            if candidate_line:
                text = content + "\n" + candidate_line
            text = text + "\n\n" + tail
            observation = cursor_execute_content(tool_result, text=text)
            with budget_lock:
                steps.append(
                    _attach_pick(
                        AgentStep(
                            step_index=len(steps),
                            tool_name=tool_name,
                            tool_args=call_args,
                            observation_text=text,
                            error=tool_result.error,
                            elapsed_ms=elapsed_ms,
                            tokens_used_delta=0,
                            tokens_used_total=budget.tokens_used,
                            ref_status=read_ref_status(tool_name, tool_result),
                            candidates=[item.handle for item in candidates] or None,
                        ),
                        handles,
                        outcome,
                    )
                )
            return observation

        custom_tools: dict[str, Any] = {}
        for spec in REGISTRY.all():
            wire_name = wire_safe_tool_name(spec.name)

            def _make_execute(resolved_name: str):
                def execute(
                    args: dict[str, Any], _ctx: Any
                ) -> str | list[dict[str, Any]]:
                    return _dispatch_sync(resolved_name, dict(args or {}))

                return execute

            custom_tools[wire_name] = cursor_sdk.CustomTool(
                execute=_make_execute(spec.name),
                description=f"{spec.description} (canonical name: {spec.name})",
                input_schema=with_pick_field(spec.json_schema),
            )

        def finish_execute(args: dict[str, Any], _ctx: Any) -> str:
            args = dict(args or {})
            handles, call_args = split_pick(args)
            validation_error = validate_finish_args(args)
            with budget_lock:
                outcome = pool.apply_pick(handles, pool.take_pending())
                if validation_error is not None:
                    steps.append(
                        _attach_pick(
                            AgentStep(
                                step_index=len(steps),
                                tool_name=FINISH_TOOL_NAME,
                                tool_args=call_args,
                                observation_text=validation_error,
                                error=validation_error,
                                elapsed_ms=0,
                                tokens_used_delta=0,
                                tokens_used_total=budget.tokens_used,
                            ),
                            handles,
                            outcome,
                        )
                    )
                    return json.dumps(
                        {
                            "status": "error",
                            "error": invalid_finish_message(validation_error),
                        }
                    )
                notes = str(call_args.get("notes") or "")
                finish_state["notes"] = notes
                steps.append(
                    _attach_pick(
                        AgentStep(
                            step_index=len(steps),
                            tool_name=FINISH_TOOL_NAME,
                            tool_args=call_args,
                            observation_text=f"pool={len(pool.entries)} notes={notes!r}",
                            error=None,
                            elapsed_ms=0,
                            tokens_used_delta=0,
                            tokens_used_total=budget.tokens_used,
                        ),
                        handles,
                        outcome,
                    )
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
                run = await agent.send(user_prompt)
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
            notes=str(finish_state["notes"] or ""),
            steps=steps,
            stop_reason=stop_reason,
            tokens_used=budget.tokens_used,
            model_name=self._model,
        )
