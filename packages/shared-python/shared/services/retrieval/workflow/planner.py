"""Query planner for decomposed retrieval workflows."""
from __future__ import annotations

import json
import re
import time
from typing import Any

from loguru import logger

from shared.services.retrieval.agentic.core.budget import BudgetExceeded, BudgetLedger
from shared.services.retrieval.llm_adapter import LLMFn, current_llm_usage
from shared.services.retrieval.workflow.types import OutputRole, PlannedStep, QueryPlan, StepKind
from shared.utils.token_estimate import estimate_tokens


_PLAN_SCHEMA = {
    "reasoning_summary": "<short summary, no hidden reasoning>",
    "steps": [
        {
            "id": "s1",
            "sub_query": "<sub-query or original query>",
            "step_kind": "retrieve",
            "depends_on": [],
            "output_role": "final_part",
            "top_k": 10,
        }
    ],
    "final_strategy": "concat_final_parts",
}

_PLANNER_PROMPT = """\
You are a retrieval workflow planner. Think step by step before answering.

User query: {query}
Document corpus inventory: {corpus_total_docs} docs / {corpus_total_chunks} chunks.
Wallet status: total_budget={total_budget} tokens (planner_used={planner_used}).

Decide whether the query needs decomposition into multiple sub-queries.
Most queries are single-step (return a 1-step plan with the original query).
Only decompose when:
  - The query asks for a comparison across distinct entities/time periods
  - The query asks for a derived computation that requires multiple facts
  - The query bundles 2+ independent informational asks

Hard constraints:
  - max_steps = {max_steps}
  - Each retrieve step costs ~{per_step_budget} tokens; do NOT plan more
    retrieve steps than the wallet can afford.
  - final_strategy must be concat_final_parts.
  - step_kind must be retrieve.
  - output_role must be final_part or intermediate.
  - KNOWHERE returns evidence only; do not plan answer synthesis steps.

Return ONLY a JSON object matching this schema (think first, then answer):
{schema}
"""


class QueryPlanner:
    """LLM-backed planner with strict fallback to a single retrieve step."""

    def __init__(
        self,
        *,
        llm_fn: LLMFn | None,
        planner_ledger: BudgetLedger | None,
        max_steps: int,
        total_budget: int,
        per_step_budget: int,
    ) -> None:
        self._llm_fn = llm_fn
        self._ledger = planner_ledger
        self._max_steps = max(max_steps, 1)
        self._total_budget = max(total_budget, 1)
        self._per_step_budget = max(per_step_budget, 1)

    async def plan(
        self,
        *,
        query: str,
        corpus_total_docs: int = 0,
        corpus_total_chunks: int = 0,
    ) -> QueryPlan:
        if self._llm_fn is None:
            return QueryPlan.single_step(query, reason="planner_llm_unavailable")

        prompt = _PLANNER_PROMPT.format(
            query=query,
            corpus_total_docs=corpus_total_docs,
            corpus_total_chunks=corpus_total_chunks,
            total_budget=self._total_budget,
            planner_used=self._planner_used(),
            max_steps=self._max_steps,
            per_step_budget=self._per_step_budget,
            schema=json.dumps(_PLAN_SCHEMA, ensure_ascii=False, indent=2),
        )

        try:
            raw = await self._call_llm_with_budget(prompt)
            plan = _parse_plan_response(
                raw,
                original_query=query,
                max_steps=self._max_steps,
            )
            plan.validate()
            return plan
        except Exception as exc:
            logger.warning(f"workflow planner failed, falling back to single step: {exc}")
            plan = QueryPlan.single_step(query, reason="planner_fallback_single_step")
            plan.planner_status = "fallback"
            plan.planner_error = str(exc)
            return plan

    def _planner_used(self) -> int:
        if self._ledger is None:
            return 0
        snapshot = self._ledger.snapshot()
        used = 0
        for pool in ("bootstrap", "planning", "context"):
            pool_state = snapshot.get(pool) or {}
            if isinstance(pool_state, dict):
                used += int(pool_state.get("used") or 0)
        return used

    async def _call_llm_with_budget(self, prompt: str) -> str:
        if self._ledger is None:
            return await self._llm_fn(prompt)  # type: ignore[misc]

        est = estimate_tokens(prompt)
        reserved = await self._ledger.try_reserve("bootstrap", est)
        if not reserved:
            raise BudgetExceeded("planner bootstrap budget exhausted")

        t0 = time.monotonic()
        try:
            response = await self._llm_fn(prompt)  # type: ignore[misc]
        except Exception:
            await self._ledger.refund("bootstrap", est=est)
            raise
        usage = current_llm_usage.get() or {}
        actual = int(usage.get("prompt_tokens") or est)
        await self._ledger.commit("bootstrap", actual=actual, est=est)
        logger.info(
            "workflow planner llm call: est_tokens={} actual_tokens={} latency={}ms",
            est,
            actual,
            int((time.monotonic() - t0) * 1000),
        )
        return response


def _parse_plan_response(text: str, *, original_query: str, max_steps: int) -> QueryPlan:
    data = _extract_json_object(text)
    if not isinstance(data, dict):
        raise ValueError("planner response is not a JSON object")

    steps_data = data.get("steps")
    if not isinstance(steps_data, list) or not steps_data:
        raise ValueError("planner response must include non-empty steps[]")
    if len(steps_data) > max_steps:
        raise ValueError(f"planner returned {len(steps_data)} steps, max is {max_steps}")

    steps: list[PlannedStep] = []
    for index, item in enumerate(steps_data, start=1):
        if not isinstance(item, dict):
            raise ValueError("planner step must be an object")
        step_id = str(item.get("id") or f"s{index}").strip()
        step_kind = _coerce_step_kind(item.get("step_kind"))
        output_role = _coerce_output_role(item.get("output_role"))
        depends_on_raw = item.get("depends_on") or []
        if not isinstance(depends_on_raw, list):
            raise ValueError(f"step {step_id} depends_on must be a list")
        top_k = item.get("top_k")
        data_type = item.get("data_type")
        steps.append(
            PlannedStep(
                id=step_id,
                sub_query=str(item.get("sub_query") or original_query).strip(),
                step_kind=step_kind,
                depends_on=[str(dep).strip() for dep in depends_on_raw if str(dep).strip()],
                output_role=output_role,
                top_k=int(top_k) if top_k is not None else None,
                data_type=int(data_type) if data_type is not None else None,
                metadata={
                    key: value
                    for key, value in item.items()
                    if key not in {"id", "sub_query", "step_kind", "depends_on", "output_role", "top_k", "data_type"}
                },
            )
        )

    return QueryPlan(
        original_query=original_query,
        steps=steps,
        final_strategy="concat_final_parts",
        reasoning_summary=str(data.get("reasoning_summary") or "").strip(),
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        parsed = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found")
    parsed = json.loads(match.group())
    if not isinstance(parsed, dict):
        raise ValueError("extracted JSON is not an object")
    return parsed


def _coerce_step_kind(value: Any) -> StepKind:
    raw = str(value or "retrieve").strip().lower()
    if raw != "retrieve":
        raise ValueError(f"unsupported step_kind: {value}")
    return raw  # type: ignore[return-value]


def _coerce_output_role(value: Any) -> OutputRole:
    raw = str(value or "final_part").strip().lower()
    if raw not in {"final_part", "intermediate"}:
        raise ValueError(f"unsupported output_role: {value}")
    return raw  # type: ignore[return-value]
