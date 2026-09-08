"""``Harness`` implementation over the Cursor SDK local agent.

Promoted from ``apps/worker/scripts/debug_cursor_agent_explore.py`` (PoC) —
see Phase 3.5 of
``.cursor/plans/agentic_corpus_explore_retrieval_c2c4ea21.plan.md``. Uses
``cursor_sdk``'s ``local.custom_tools`` so this host process still executes
``agent_tools.REGISTRY`` (same ``corpus.*`` tools as ``harness/openai_harness.py``),
while the orchestration model comes from Cursor (default
``config.AGENT_EXPLORE_CURSOR_MODEL``, e.g. ``composer-2.5``) instead of
DeepSeek.

Requires the optional ``cursor-sdk`` dependency (see
``apps/worker/pyproject.toml``'s ``cursor-harness`` extra) and
``CURSOR_API_KEY``. The guarded import in ``_require_cursor_sdk`` raises a
clear, actionable error at harness-selection time if ``cursor-sdk`` isn't
installed, instead of failing deep inside a running episode.

Architectural difference from ``openai_harness.py`` that budget enforcement
has to work around: this harness does not control the LLM turn loop.
``agent.send(...)`` + ``await run.wait()`` hands the *entire* multi-turn
tool-calling loop to the Cursor SDK; the host process only sees (a)
``execute`` callbacks for each ``corpus.*``/``finish`` tool call — run
synchronously off the SDK's own thread and bridged back onto this event
loop via ``asyncio.run_coroutine_threadsafe`` (mirrors the PoC's
``_dispatch_sync``) — and (b) the terminal ``RunResult`` once ``wait()``
returns. There is no per-turn hook to inspect budget mid-turn and force
``tool_choice`` the way ``openai_harness.py`` does. Each budget dimension is
therefore enforced (or explicitly not) differently here:

- **``max_steps``**: a plain counter (``budget.steps_used``, incremented once
  per ``corpus.*`` dispatch — ``finish`` does not count, it ends the episode
  on its own) checked in ``_dispatch_sync`` *before* dispatching. Once the
  counter reaches ``budget.max_steps``, further ``corpus.*`` calls are not
  forwarded to ``REGISTRY`` at all — the callback returns a fixed
  "budget exhausted, call finish now" string instead. This is a real,
  synchronous cutoff (unlike the two dimensions below): no reliance on
  cancelling the SDK run from a background task.
- **``wall_clock``**: ``asyncio.wait_for(run.wait(), timeout=budget.wall_clock_seconds)``,
  per the plan. On timeout, best-effort ``await run.cancel()`` (own short
  timeout, so a hung cancel RPC can't hang this call forever) so the
  underlying agent run actually stops server-side instead of merely being
  abandoned by this process, then the episode result is built from whatever
  ``finish``/tool-trajectory refs were captured before the timeout.
- **``token_limit``**: **not actively enforced mid-run.** Verified by reading
  the installed ``cursor-sdk`` package's source
  (``cursor_sdk._run_base._RunBase.usage``, 2026-09) that cumulative token
  usage is incrementally accumulated from ``SDKUsageMessage`` stream events
  as ``run.wait()`` consumes them, and exposed as a live ``run.usage``
  property — so a mid-run cutoff (e.g. a polling task calling
  ``run.cancel()``) is *plausible*. It was **not** exercised against a real
  ``CURSOR_API_KEY`` run in this change (no key available in the
  implementation environment), so building an active cutoff on top of an
  unverified live-update assumption was judged higher-risk than shipping.
  ``EpisodeBudget.exhausted()``'s ``token_limit`` dimension is therefore left
  unchecked here by design; ``budget.tokens_used`` is only populated
  *post-hoc* from the terminal ``RunResult.usage`` for observability
  (``EpisodeResult.tokens_used``), after the episode has already finished.
  If ``eval-cursor-harness`` confirms ``run.usage`` does update live against
  a real key, promoting this to an active cutoff (mirroring the
  ``max_steps``/``wall_clock`` pattern above) is straightforward follow-up
  work — deliberately not done speculatively here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from typing import Any

from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_explore.config import (
    AGENT_EXPLORE_CURSOR_MODEL,
    FINISH_TOOL_DESCRIPTION,
    FINISH_TOOL_NAME,
    FINISH_TOOL_SCHEMA,
    LOOP_CONTRACT_SUFFIX,
)
from shared.services.retrieval.agent_explore.dispatch import DbFactory, dispatch_tool_call
from shared.services.retrieval.agent_explore.shared import (
    EVIDENCE_TOOL_NAMES,
    budget_status_line,
    dedup_refs,
    normalize_finish_refs,
    tool_message_content,
    wire_safe_tool_name,
)
from shared.services.retrieval.agent_explore.types import AgentStep, EpisodeResult
from shared.services.retrieval.agent_tools import (
    REGISTRY,
    ToolBudget,
    ToolResult,
    load_corpus_schema_text,
)
from shared.services.retrieval.agent_tools import tools as _agent_tools_registered  # noqa: F401

# Grace period for the underlying agent run to actually stop, after a
# best-effort run.cancel() following a wall_clock timeout, before this
# process gives up waiting for a terminal RunResult and falls back to
# whatever refs the trajectory already captured. Not tuned against real
# cancel-RPC latency yet (no CURSOR_API_KEY in the implementation
# environment) — revisit with eval-cursor-harness data.
_CANCEL_GRACE_SECONDS = 30.0

_BUDGET_EXHAUSTED_MESSAGE = (
    "error: step budget exhausted for this episode — do not call any more "
    "corpus.* tools; call finish now with whatever refs you already have "
    "(or an empty list plus a notes explanation)."
)


def _require_cursor_sdk() -> Any:
    try:
        import cursor_sdk
    except ImportError as exc:
        raise RuntimeError(
            "AGENT_EXPLORE_HARNESS=cursor_sdk requires the optional "
            "'cursor-sdk' dependency, which is not installed in this "
            "interpreter. Install with: uv sync --extra cursor-harness "
            "(apps/worker/pyproject.toml)."
        ) from exc
    return cursor_sdk


class CursorHarness:
    """``Harness`` implementation over the Cursor SDK local agent."""

    def __init__(self, *, model: str | None = None) -> None:
        self._model = model or AGENT_EXPLORE_CURSOR_MODEL

    async def run_episode(
        self,
        *,
        db_factory: DbFactory,
        user_id: str,
        namespace: str,
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

        steps: list[AgentStep] = []
        trajectory_refs: list[dict[str, Any]] = []
        # None until finish is actually called — distinguishes "finish
        # called with an empty refs list" (respect it) from "finish never
        # called" (fall back to trajectory_refs below), same contract as
        # openai_harness.py's normalize_finish_refs + fallback.
        finish_state: dict[str, Any] = {"refs": None, "notes": ""}
        stop_reason = "finished"

        def _dispatch_sync(tool_name: str, args: dict[str, Any]) -> str:
            if budget.steps_used >= budget.max_steps:
                steps.append(
                    AgentStep(
                        step_index=len(steps),
                        tool_name=tool_name,
                        tool_args=args,
                        observation_text=_BUDGET_EXHAUSTED_MESSAGE,
                        error="budget_max_steps",
                        elapsed_ms=0,
                        tokens_used_delta=0,
                        tokens_used_total=budget.tokens_used,
                    )
                )
                return _BUDGET_EXHAUSTED_MESSAGE
            budget.record_step()
            tool_started = time.perf_counter()
            future = asyncio.run_coroutine_threadsafe(
                dispatch_tool_call(
                    tool_name,
                    args,
                    db_factory=db_factory,
                    user_id=user_id,
                    namespace=namespace,
                    budget=tool_budget,
                ),
                loop,
            )
            try:
                tool_result = future.result(timeout=180)
            except Exception as exc:  # noqa: BLE001 - one broken tool must not kill the episode
                tool_result = ToolResult(text="", error=f"{type(exc).__name__}: {exc}")
            elapsed_ms = int((time.perf_counter() - tool_started) * 1000)
            content = tool_message_content(tool_result, max_chars=tool_budget.max_chars)
            # Appended per call, unlike openai_harness.py's once-per-turn
            # placement — this harness has no batched-turn concept exposed to
            # the host process (see module docstring): each corpus.* dispatch
            # is the only per-step hook available to surface budget state.
            content_with_budget = content + "\n" + budget_status_line(budget)
            steps.append(
                AgentStep(
                    step_index=len(steps),
                    tool_name=tool_name,
                    tool_args=args,
                    observation_text=content_with_budget,
                    error=tool_result.error,
                    elapsed_ms=elapsed_ms,
                    tokens_used_delta=0,
                    tokens_used_total=budget.tokens_used,
                )
            )
            if tool_name in EVIDENCE_TOOL_NAMES and not tool_result.error:
                trajectory_refs.extend(tool_result.refs)
            return content_with_budget

        custom_tools: dict[str, Any] = {}
        for spec in REGISTRY.all():
            wire_name = wire_safe_tool_name(spec.name)

            def _make_execute(resolved_name: str):
                def execute(args: dict[str, Any], _ctx: Any) -> str:
                    return _dispatch_sync(resolved_name, dict(args or {}))

                return execute

            custom_tools[wire_name] = cursor_sdk.CustomTool(
                execute=_make_execute(spec.name),
                description=f"{spec.description} (canonical name: {spec.name})",
                input_schema=spec.json_schema,
            )

        def finish_execute(args: dict[str, Any], _ctx: Any) -> str:
            finish_state["refs"] = normalize_finish_refs(args.get("refs"))
            finish_state["notes"] = str(args.get("notes") or "")
            return json.dumps({"status": "finished", "refs": len(finish_state["refs"])})

        custom_tools[FINISH_TOOL_NAME] = cursor_sdk.CustomTool(
            execute=finish_execute,
            description=FINISH_TOOL_DESCRIPTION,
            input_schema=FINISH_TOOL_SCHEMA,
        )

        system_prompt = load_corpus_schema_text() + LOOP_CONTRACT_SUFFIX
        user_prompt = (
            f"{system_prompt}\n\n---\n\nUser query:\n{query}\n\n"
            "Tool names on the wire use underscores "
            f"({', '.join(sorted(custom_tools))}). Explore with those tools, "
            "then call finish with cited refs."
        )

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

        result_refs = finish_state["refs"]
        result_notes = str(finish_state["notes"] or "")
        if not result_refs:
            fallback_refs = dedup_refs(trajectory_refs)
            if fallback_refs:
                result_refs = fallback_refs
                result_notes = (result_notes + " " if result_notes else "") + (
                    "[refs auto-filled from corpus.read/corpus.assets trajectory; "
                    "finish did not cite any]"
                )
        result_refs = result_refs or []

        return EpisodeResult(
            refs=result_refs,
            notes=result_notes,
            steps=steps,
            stop_reason=stop_reason,
            tokens_used=budget.tokens_used,
            model_name=self._model,
        )
