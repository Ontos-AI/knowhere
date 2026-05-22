"""LLM-guided long-PDF shard planning from candidate split pages."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from app.services.document_agent.manifest import (
    BoundaryCandidate,
    Shard,
    ShardPlan,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.registry import register_tool
from app.services.document_agent.state import DocumentAgentState
from app.services.document_agent.validators import single_shard_plan, validate_shard_plan
from shared.utils.token_estimate import estimate_tokens


def _thresholds(ctx: ToolContext) -> tuple[int, int, int]:
    threshold = int(
        ctx.settings.get("shard_threshold")
        or os.environ.get("PARSE_AGENT_SHARD_THRESHOLD", "200")
    )
    min_pages = int(
        ctx.settings.get("min_pages_per_shard")
        or os.environ.get("PARSE_AGENT_MIN_PAGES_PER_SHARD", "20")
    )
    max_pages = int(
        ctx.settings.get("max_pages_per_shard")
        or os.environ.get("PARSE_AGENT_MAX_PAGES_PER_SHARD", "200")
    )
    return threshold, min_pages, max_pages


def _cuts_to_shards(cuts: list[tuple[int, str, str, float]], page_count: int) -> list[Shard]:
    shards: list[Shard] = []
    previous = 0
    for cut_page, anchor_type, evidence, confidence in cuts:
        if cut_page <= previous:
            continue
        shards.append(
            Shard(
                shard_index=len(shards),
                page_start=previous + 1,
                page_end=cut_page,
                page_offset=previous,
                anchor_type=anchor_type,  # type: ignore[arg-type]
                anchor_evidence=evidence,
                confidence=confidence,
            )
        )
        previous = cut_page
    if previous < page_count:
        shards.append(
            Shard(
                shard_index=len(shards),
                page_start=previous + 1,
                page_end=page_count,
                page_offset=previous,
                anchor_type="forced_max_size",
                anchor_evidence="final shard",
                confidence=1.0,
            )
        )
    return shards


def _compact_candidate(candidate: BoundaryCandidate, page_count: int) -> dict[str, Any]:
    evidence = candidate.evidence or {}
    return {
        "page": candidate.page,
        "kind": candidate.kind,
        "priority": candidate.priority,
        "confidence": candidate.confidence,
        "position_ratio": round(candidate.page / max(page_count, 1), 4),
        "raw_text_length": evidence.get("raw_text_length"),
        "image_coverage": evidence.get("image_coverage"),
        "table_count": evidence.get("table_count"),
        "drawings_count": evidence.get("drawings_count"),
        "text_preview": evidence.get("text_preview", [])[:3],
        "title": evidence.get("title"),
    }


def _build_prompt(
    *,
    page_count: int,
    min_pages: int,
    max_pages: int,
    candidates: list[BoundaryCandidate],
    page_kind_counts: dict[str, int],
) -> str:
    payload = {
        "page_count": page_count,
        "min_pages_per_shard": min_pages,
        "max_pages_per_shard": max_pages,
        "page_kind_counts": page_kind_counts,
        "candidate_priority": {
            "h1": "highest semantic priority, but still decide using document size and spacing",
            "toc": "high priority marker, usually not a cut by itself unless it indicates nearby structure",
            "separator": "explicit sparse separator page",
            "blank": "sparse/blank structural gap candidate",
            "sparse": "low-density candidate, useful when no stronger signal exists",
        },
        "candidates": [
            _compact_candidate(candidate, page_count) for candidate in candidates
        ],
    }
    return (
        "You are a senior document parsing architect. Decide whether to split a PDF "
        "and where to split it using only the provided candidate pages and document-scale "
        "features.\n"
        "Rules:\n"
        "- Return strict JSON only.\n"
        "- Do not invent pages. Every cut_after_page must be one of the candidate pages, "
        "or candidate_page - 1 when the candidate is a semantic start page such as h1.\n"
        "- H1 candidates have the highest semantic priority, but do not blindly split on "
        "every H1. Consider total page_count, candidate spacing, min/max shard sizes, and "
        "whether splitting would over-fragment the document.\n"
        "- Blank and sparse pages are valid split candidates because they often mark section "
        "gaps, especially when TOC/H1 evidence is weak or absent.\n"
        "- Prefer fewer, semantically coherent shards over many tiny shards.\n"
        "- Do not use domain-specific hardcoded labels or examples; decide from the supplied "
        "features and positions only. Do not quote business/category words from text_preview "
        "in rationale; refer to them generically as sparse separator text.\n"
        "- Every resulting shard length must be <= max_pages_per_shard unless enabled=false. "
        "Check each segment length exactly before returning.\n"
        "- If no split is useful, return enabled=false and cuts=[] even for a long document.\n"
        "Output schema:\n"
        "{\n"
        '  "enabled": boolean,\n'
        '  "cuts": [\n'
        "    {\"cut_after_page\": number, \"anchor_type\": \"h1_boundary\" | "
        "\"blank_separator\" | \"separator\" | \"forced_max_size\", "
        "\"confidence\": number, \"rationale\": string}\n"
        "  ],\n"
        '  "reason": "llm_boundary_decision" | "not_needed" | "too_large",\n'
        '  "rationale": string\n'
        "}\n"
        "Payload:\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _sanitize_rationale(text: str) -> str:
    # Keep rationales structural. The model may quote page preview text; those
    # literals are useful as input evidence but should not become baked-in rules.
    sanitized = re.sub(r"'[^']{1,40}'", "sparse separator text", text or "")
    sanitized = re.sub(r'"[^"]{1,40}"', "sparse separator text", sanitized)
    return sanitized


def _validate_cut_lengths(cuts: list[tuple[int, str, str, float]], page_count: int, max_pages: int) -> None:
    previous = 0
    for cut_page, *_ in cuts:
        if cut_page - previous > max_pages:
            raise ValueError(
                f"LLM cut plan creates shard length {cut_page - previous} > max_pages={max_pages}"
            )
        previous = cut_page
    if page_count - previous > max_pages:
        raise ValueError(
            f"LLM cut plan creates final shard length {page_count - previous} > max_pages={max_pages}"
        )


def _parse_llm_plan(raw: str, page_count: int, max_pages: int) -> tuple[bool, list[tuple[int, str, str, float]], str, str]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("LLM shard plan is not an object")
    enabled = bool(data.get("enabled"))
    reason = str(data.get("reason") or ("llm_boundary_decision" if enabled else "not_needed"))
    rationale = _sanitize_rationale(str(data.get("rationale") or ""))
    cuts: list[tuple[int, str, str, float]] = []
    for item in data.get("cuts") or []:
        if not isinstance(item, dict):
            continue
        cut_page = int(item.get("cut_after_page"))
        if not 1 <= cut_page < page_count:
            continue
        anchor_type = str(item.get("anchor_type") or "separator")
        if anchor_type not in {"h1_boundary", "blank_separator", "separator", "forced_max_size"}:
            anchor_type = "separator"
        confidence = float(item.get("confidence") or 0.5)
        cuts.append((cut_page, anchor_type, _sanitize_rationale(str(item.get("rationale") or rationale)), confidence))
    cuts = sorted({cut[0]: cut for cut in cuts}.values(), key=lambda cut: cut[0])
    if enabled:
        _validate_cut_lengths(cuts, page_count, max_pages)
    return enabled, cuts, reason, rationale


def _deterministic_guardrail_plan(
    *,
    page_count: int,
    max_pages: int,
    candidates: list[BoundaryCandidate],
) -> tuple[list[tuple[int, str, str, float]], str]:
    cuts: list[tuple[int, str, str, float]] = []
    previous = 0
    while page_count - previous > max_pages:
        target = previous + max_pages
        eligible = [
            candidate for candidate in candidates if previous < candidate.page <= target
        ]
        if eligible:
            chosen = max(eligible, key=lambda item: (item.priority, item.page))
            cut_page = chosen.page - 1 if chosen.kind == "h1" and chosen.page > previous + 1 else chosen.page
            anchor_type = "h1_boundary" if chosen.kind == "h1" else "blank_separator"
            cuts.append((cut_page, anchor_type, f"guardrail candidate {chosen.kind} at page {chosen.page}", 0.35))
            previous = cut_page
        else:
            cuts.append((target, "forced_max_size", "guardrail max shard size", 0.25))
            previous = target
    return cuts, "too_large"


@register_tool(
    name="propose.shard_plan",
    description="Ask the LLM to decide whether and where to split using candidate boundary pages.",
    allowed_states={DocumentAgentState.H1_FOUND},
)
def propose_shard_plan(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
    start = time.monotonic()
    page_count = ctx.blackboard.page_count
    threshold, min_pages, max_pages = _thresholds(ctx)
    if page_count <= threshold:
        plan = single_shard_plan(page_count)
        ctx.blackboard.shard_plan = plan
        return ToolResult(
            status="ok",
            payload={"enabled": False, "shard_count": len(plan.shards)},
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    candidates = list(ctx.blackboard.boundary_candidates)
    model = ctx.settings.get("model")
    prompt = _build_prompt(
        page_count=page_count,
        min_pages=min_pages,
        max_pages=max_pages,
        candidates=candidates,
        page_kind_counts=ctx.blackboard.global_signals.get("page_kind_counts", {}),
    )
    prompt_tokens_est = estimate_tokens(prompt)
    warnings: list[str] = []
    raw_response = ""
    rationale = ""
    llm_attempted = False
    if model and ctx.budget.try_reserve("plan", prompt_tokens_est):
        try:
            llm_attempted = True
            from shared.services.ai.openai_compatible_client_sync import get_openai_client

            client = get_openai_client(model=model)
            raw_response, usage = client.chat_completion_with_usage(
                messages=prompt,
                model=model,
                temperature=0.0,
                max_tokens=1600,
                response_format={"type": "json_object"},
            )
            ctx.budget.commit("plan", actual=usage.get("total_tokens", prompt_tokens_est), est=prompt_tokens_est)
            enabled, cuts, reason, rationale = _parse_llm_plan(raw_response, page_count, max_pages)
            if not enabled:
                cuts = []
                reason = "not_needed"
        except Exception as exc:
            ctx.budget.refund("plan", est=prompt_tokens_est)
            warnings.append(f"LLM shard decision rejected, using guardrail plan: {exc}")
            cuts, reason = _deterministic_guardrail_plan(
                page_count=page_count,
                max_pages=max_pages,
                candidates=candidates,
            )
            rationale = "Guardrail plan after LLM shard decision failure."
    else:
        if not model:
            warnings.append("No model configured for shard decision; using guardrail plan.")
        else:
            warnings.append("Insufficient plan budget for shard decision; using guardrail plan.")
        cuts, reason = _deterministic_guardrail_plan(
            page_count=page_count,
            max_pages=max_pages,
            candidates=candidates,
        )
        rationale = "Guardrail plan without LLM decision."

    shards = _cuts_to_shards(cuts, page_count)
    enabled = len(shards) > 1
    if not enabled:
        reason = "not_needed"
    plan = ShardPlan(
        enabled=enabled,
        reason=reason,  # type: ignore[arg-type]
        shards=shards,
        validation=validate_shard_plan(
            ShardPlan(enabled=enabled, reason=reason, shards=shards),  # type: ignore[arg-type]
            page_count=page_count,
            min_pages=min_pages,
            max_pages=max_pages,
        ),
    )
    ctx.blackboard.shard_plan = plan
    return ToolResult(
        status="ok",
        payload={
            "enabled": plan.enabled,
            "reason": plan.reason,
            "shard_count": len(plan.shards),
            "valid": plan.validation.valid,
        },
        latency_ms=int((time.monotonic() - start) * 1000),
        tokens_used=ctx.budget.snapshot()["plan"]["used"],
        input_summary={
            "page_count": page_count,
            "candidate_count": len(candidates),
            "candidate_counts": ctx.blackboard.global_signals.get("boundary_candidate_counts", {}),
            "model": model,
        },
        output_summary={
            "enabled": plan.enabled,
            "reason": plan.reason,
            "rationale": rationale,
            "shards": [shard.to_dict() for shard in plan.shards],
        },
        warnings=warnings,
        debug={
            "prompt_excerpt": prompt[:4000],
            "raw_response_excerpt": raw_response[:4000],
            "llm_attempted": llm_attempted,
        },
    )
