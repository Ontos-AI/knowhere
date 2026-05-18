"""Synthesis helpers for decomposed retrieval workflows."""
from __future__ import annotations

import re

from shared.services.retrieval.agentic.core.budget import BudgetLedger
from shared.services.retrieval.llm_adapter import LLMFn, current_llm_usage
from shared.services.retrieval.workflow.types import PlannedStep, QueryPlan, StepResult
from shared.utils.token_estimate import estimate_tokens


_SYNTHESIZE_PROMPT = """\
You are composing an intermediate or final answer for a retrieval workflow.
Use ONLY the prior step outputs below. Do not use external knowledge.

Current step id: {step_id}
Current step task: {sub_query}

Prior step outputs:
{prior_outputs}

Return a concise answer that directly satisfies the current step task.
If the prior outputs are insufficient, explain the missing information.
"""


async def synthesize_step(
    step: PlannedStep,
    *,
    prior_results: dict[str, StepResult],
    llm_fn: LLMFn,
    ledger: BudgetLedger | None,
) -> str:
    prior_outputs = _format_prior_outputs(step.depends_on, prior_results)
    prompt = _SYNTHESIZE_PROMPT.format(
        step_id=step.id,
        sub_query=step.sub_query,
        prior_outputs=prior_outputs,
    )
    if ledger is None:
        return (await llm_fn(prompt)).strip()

    est = estimate_tokens(prompt)
    reserved = await ledger.try_reserve("context", est)
    if not reserved:
        raise RuntimeError("synthesis context budget exhausted")
    try:
        response = await llm_fn(prompt)
    except Exception:
        await ledger.refund("context", est=est)
        raise
    usage = current_llm_usage.get() or {}
    actual = int(usage.get("prompt_tokens") or est)
    await ledger.commit("context", actual=actual, est=est)
    return response.strip()


def compose_final_answer(plan: QueryPlan, results: dict[str, StepResult]) -> str:
    """Compose workflow final answer according to planner strategy."""
    if plan.final_strategy == "last_synthesize":
        for step in reversed(plan.steps):
            result = results.get(step.id)
            if result and step.step_kind == "synthesize" and result.status == "done":
                return result.answer_text
        return _concat_final_parts(plan, results)

    if plan.final_strategy == "template" and plan.final_template:
        return _render_template(plan.final_template, results).strip()

    return _concat_final_parts(plan, results)


def _concat_final_parts(plan: QueryPlan, results: dict[str, StepResult]) -> str:
    parts: list[str] = []
    for step in plan.steps:
        if step.output_role != "final_part":
            continue
        result = results.get(step.id)
        if not result or result.status not in ("done", "budget_stop") or not result.answer_text:
            continue
        parts.append(result.answer_text.strip())
    if parts:
        return "\n\n".join(parts)

    fallback_parts = [
        result.answer_text.strip()
        for step in plan.steps
        if (result := results.get(step.id)) and result.answer_text.strip()
    ]
    if fallback_parts:
        return "\n\n".join(fallback_parts)

    missing_reasons = [
        result.failure_reason.strip()
        for step in plan.steps
        if (result := results.get(step.id))
        and result.status == "not_found"
        and result.failure_reason.strip()
    ]
    if missing_reasons:
        return "未能基于当前知识库证据回答该问题：" + "；".join(dict.fromkeys(missing_reasons))

    if any((result := results.get(step.id)) and result.status == "budget_stop" for step in plan.steps):
        return "Unable to return a valid answer because the retrieval budget was exhausted."

    return ""


def _format_prior_outputs(depends_on: list[str], prior_results: dict[str, StepResult]) -> str:
    lines: list[str] = []
    for step_id in depends_on:
        result = prior_results.get(step_id)
        if not result:
            lines.append(f"## {step_id}\n(status: missing)\n")
            continue
        lines.append(
            "\n".join(
                [
                    f"## {step_id}",
                    f"Sub-query: {result.sub_query}",
                    f"Status: {result.status}",
                    "Answer:",
                    result.answer_text or "(empty)",
                    "",
                ]
            )
        )
    return "\n".join(lines) if lines else "(no prior outputs)"


def _render_template(template: str, results: dict[str, StepResult]) -> str:
    def _replace(match: re.Match[str]) -> str:
        step_id = match.group(1)
        field = match.group(2)
        result = results.get(step_id)
        if not result:
            return ""
        attr = "answer_text" if field == "answer" else field
        return str(getattr(result, attr, ""))

    return re.sub(r"\{\{\s*steps\.([A-Za-z0-9_-]+)\.(answer_text|answer|evidence_text|status)\s*\}\}", _replace, template)
