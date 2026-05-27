"""LLM-guided long-PDF shard planning from document profile evidence."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from app.services.document_agent.manifest import (
    Shard,
    ShardPlan,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.registry import has_doc_stats, has_h1_result, has_toc_result, register_tool
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


def _build_prompt(
    *,
    page_count: int,
    min_pages: int,
    max_pages: int,
    doc_stats: dict[str, Any],
    page_kind_counts: dict[str, int],
    toc_pages: list[int],
    h1_pages: list[dict[str, Any]],
    profile: dict[str, Any] | None,
    visual_evidence: list[dict[str, Any]],
    grep_history: list[dict[str, Any]],
) -> str:
    payload = {
        "page_count": page_count,
        "min_pages_per_shard": min_pages,
        "max_pages_per_shard": max_pages,
        "page_kind_counts": page_kind_counts,
        "doc_stats": doc_stats,
        "toc_pages": toc_pages,
        "h1_pages": h1_pages,
        "document_profile": profile,
        "visual_evidence": visual_evidence[-3:],
        "grep_history": grep_history[-3:],
    }
    return (
        "You are a senior document parsing architect. Decide whether to split a PDF "
        "and where to split it using document-scale features, TOC/H1 evidence, and "
        "recent agent observations.\n"
        "Rules:\n"
        "- Return strict JSON only.\n"
        "- Prefer H1 start pages as semantic boundaries, cutting at page-1 when possible.\n"
        "- Do not blindly split on every H1. Consider total page_count, spacing, min/max "
        "shard sizes, and over-fragmentation.\n"
        "- Prefer fewer, semantically coherent shards over many tiny shards.\n"
        "- Keep each cut rationale under 120 characters.\n"
        "- Every resulting shard length must be between min_pages_per_shard and "
        "max_pages_per_shard, except the final shard may be shorter only when no better "
        "valid split exists. Check each segment length exactly before returning.\n"
        "- If no split is useful, return enabled=false and cuts=[] even for a long document.\n"
        "Output schema:\n"
        "{\n"
        '  "enabled": boolean,\n'
        '  "cuts": [\n'
        "    {\"cut_after_page\": number, \"anchor_type\": \"h1_boundary\" | "
        "\"blank_separator\" | \"forced_max_size\", "
        "\"confidence\": number, \"rationale\": string}\n"
        "  ],\n"
        '  "reason": "llm_boundary_decision" | "not_needed" | "too_large",\n'
        '  "rationale": string\n'
        "}\n"
        "Payload:\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _sanitize_rationale(text: str, max_length: int = 120) -> str:
    # Truncate overlong rationales but preserve H1 title references
    # which provide valuable semantic context for shard boundaries.
    sanitized = (text or "").strip()
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length].rstrip() + "…"
    return sanitized


def _validate_cut_lengths(
    cuts: list[tuple[int, str, str, float]],
    page_count: int,
    min_pages: int,
    max_pages: int,
) -> None:
    previous = 0
    for cut_page, *_ in cuts:
        if cut_page - previous < min_pages:
            raise ValueError(
                f"LLM cut plan creates shard length {cut_page - previous} < min_pages={min_pages}"
            )
        if cut_page - previous > max_pages:
            raise ValueError(
                f"LLM cut plan creates shard length {cut_page - previous} > max_pages={max_pages}"
            )
        previous = cut_page
    if page_count - previous < min_pages and cuts:
        raise ValueError(
            f"LLM cut plan creates final shard length {page_count - previous} < min_pages={min_pages}"
        )
    if page_count - previous > max_pages:
        raise ValueError(
            f"LLM cut plan creates final shard length {page_count - previous} > max_pages={max_pages}"
        )


def _parse_llm_plan(
    raw: str,
    page_count: int,
    min_pages: int,
    max_pages: int,
) -> tuple[bool, list[tuple[int, str, str, float]], str, str]:
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
        raw_cut_page = item.get("cut_after_page")
        if raw_cut_page is None:
            continue
        cut_page = int(raw_cut_page)
        if not 1 <= cut_page < page_count:
            continue
        anchor_type = str(item.get("anchor_type") or "forced_max_size")
        if anchor_type not in {"h1_boundary", "blank_separator", "forced_max_size"}:
            anchor_type = "forced_max_size"
        confidence = float(item.get("confidence") or 0.5)
        cuts.append((cut_page, anchor_type, _sanitize_rationale(str(item.get("rationale") or rationale)), confidence))
    cuts = sorted({cut[0]: cut for cut in cuts}.values(), key=lambda cut: cut[0])
    if enabled:
        _validate_cut_lengths(cuts, page_count, min_pages, max_pages)
    return enabled, cuts, reason, rationale


def _deterministic_guardrail_plan(
    *,
    page_count: int,
    min_pages: int,
    max_pages: int,
    h1_pages: list[int],
) -> tuple[list[tuple[int, str, str, float]], str]:
    cuts: list[tuple[int, str, str, float]] = []
    previous = 0
    while page_count - previous > max_pages:
        target = previous + max_pages
        eligible = [
            page for page in h1_pages if previous + 1 < page <= target
        ]
        if eligible:
            chosen = max(eligible)
            cut_page = chosen - 1
            cuts.append((cut_page, "h1_boundary", f"guardrail H1 start page {chosen}", 0.35))
            previous = cut_page
        else:
            cuts.append((target, "forced_max_size", "guardrail max shard size", 0.25))
            previous = target
    # Merge final shard into previous if it's smaller than min_pages
    if cuts and (page_count - cuts[-1][0]) < min_pages:
        cuts.pop()
    return cuts, "too_large"


@register_tool(
    name="propose.shard_plan",
    description="Ask the LLM to decide whether and where to split using profile, TOC, and H1 evidence.",
    preconditions=(has_doc_stats, has_toc_result, has_h1_result),
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

    h1_candidates = (
        ctx.blackboard.h1_result.h1_candidates if ctx.blackboard.h1_result else []
    )
    h1_pages = [{"title": item.title, "page": item.page} for item in h1_candidates]
    model = ctx.settings.get("model")
    prompt = _build_prompt(
        page_count=page_count,
        min_pages=min_pages,
        max_pages=max_pages,
        doc_stats=ctx.blackboard.doc_stats,
        page_kind_counts=ctx.blackboard.global_signals.get("page_kind_counts", {}),
        toc_pages=ctx.blackboard.toc_result.toc_pages if ctx.blackboard.toc_result else [],
        h1_pages=h1_pages,
        profile=ctx.blackboard.document_profile.to_dict()
        if ctx.blackboard.document_profile
        else None,
        visual_evidence=ctx.blackboard.global_signals.get("visual_inspections", []),
        grep_history=ctx.blackboard.global_signals.get("grep_history", []),
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
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.0,
                max_tokens=1600,
                response_format={"type": "json_object"},
            )
            ctx.budget.commit("plan", actual=usage.get("total_tokens", prompt_tokens_est), est=prompt_tokens_est)
            enabled, cuts, reason, rationale = _parse_llm_plan(raw_response, page_count, min_pages, max_pages)
            if not enabled:
                cuts = []
                reason = "not_needed"
        except Exception as exc:
            ctx.budget.refund("plan", est=prompt_tokens_est)
            warnings.append(f"LLM shard decision failed; using guardrail plan: {exc}")
            ctx.blackboard.global_signals.setdefault("degraded_reasons", []).append(
                "shard_plan: llm_parse_failed"
            )
            cuts, reason = _deterministic_guardrail_plan(
                page_count=page_count,
                min_pages=min_pages,
                max_pages=max_pages,
                h1_pages=[item["page"] for item in h1_pages],
            )
            rationale = "Guardrail plan after malformed LLM shard decision."
    else:
        if not model:
            warnings.append("No model configured for shard decision; using guardrail plan.")
            ctx.blackboard.global_signals.setdefault("degraded_reasons", []).append(
                "shard_plan: no model"
            )
            cuts, reason = _deterministic_guardrail_plan(
                page_count=page_count,
                min_pages=min_pages,
                max_pages=max_pages,
                h1_pages=[item["page"] for item in h1_pages],
            )
            rationale = "Guardrail plan without configured shard model."
        else:
            return ToolResult(
                status="error",
                error="Insufficient plan budget for shard decision.",
                latency_ms=int((time.monotonic() - start) * 1000),
                warnings=warnings,
                debug={
                    "prompt_excerpt": prompt[:4000],
                    "raw_response_excerpt": raw_response[:4000],
                    "llm_attempted": llm_attempted,
                },
            )

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
            "h1_count": len(h1_pages),
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
