"""LLM-guided long-PDF shard planning from document profile evidence."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from app.services.document_agent.manifest import (
    H1Candidate,
    Shard,
    ShardPlan,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.pdf_text import read_page_texts
from app.services.document_agent.registry import has_doc_stats, has_h1_result, has_toc_result, register_tool
from app.services.document_agent.tools.match_h1_pages import (
    extract_children_titles,
    grep_titles_in_pages,
    verify_section_start,
)
from app.services.document_agent.validators import single_shard_plan, validate_shard_plan
from loguru import logger
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
    # Track which cuts came from H2 refinement to mark continuation shards
    h2_cut_pages: set[int] = set()
    for cut_page, _anchor_type, evidence, _confidence in cuts:
        if evidence.startswith("H2 refine:"):
            h2_cut_pages.add(cut_page)

    for cut_page, anchor_type, evidence, confidence in cuts:
        if cut_page <= previous:
            continue
        # A shard is continuation if it starts AFTER an H2 cut (previous cut was H2)
        _is_continuation = previous in h2_cut_pages
        _split_depth = 2 if (evidence.startswith("H2 refine:") or _is_continuation) else 1
        shards.append(
            Shard(
                shard_index=len(shards),
                page_start=previous + 1,
                page_end=cut_page,
                page_offset=previous,
                anchor_type=anchor_type,  # type: ignore[arg-type]
                anchor_evidence=evidence,
                confidence=confidence,
                split_depth=_split_depth,
                is_continuation=_is_continuation,
            )
        )
        previous = cut_page
    if previous < page_count:
        _is_continuation = previous in h2_cut_pages
        shards.append(
            Shard(
                shard_index=len(shards),
                page_start=previous + 1,
                page_end=page_count,
                page_offset=previous,
                anchor_type="forced_max_size",
                anchor_evidence="final shard",
                confidence=1.0,
                split_depth=2 if _is_continuation else 1,
                is_continuation=_is_continuation,
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
            break  # No more H1 in range → leave oversized shard for H2 refinement
    return cuts, "too_large"


# ── C3: H2-aware shard refinement ────────────────────────────────────────


def _find_h1_for_range(
    h1_candidates: list[H1Candidate],
    range_start: int,
    range_end: int,
) -> str | None:
    """Find the H1 title whose start page falls in [range_start+1, range_end]."""
    for c in h1_candidates:
        if range_start < c.page <= range_end:
            return c.title
    # Fallback: the H1 whose page is closest to and <= range_start+1
    best: H1Candidate | None = None
    for c in h1_candidates:
        if c.page <= range_start + 1:
            if best is None or c.page > best.page:
                best = c
    return best.title if best else None


def _pick_and_verify_best_cut(
    h2_candidates: list[H1Candidate],
    shard_start: int,
    shard_end: int,
    min_pages: int,
    max_pages: int,
    ctx: ToolContext,
) -> tuple[int, str, str, float] | None:
    """Pick the H2 candidate that produces the most balanced sub-shards.

    Candidates are ranked by how close they split the shard to the midpoint.
    Each candidate is VLM-verified before acceptance.
    """
    if not h2_candidates:
        return None

    shard_length = shard_end - shard_start
    midpoint = shard_start + shard_length // 2

    # Sort by distance to midpoint (most balanced first)
    ranked = sorted(h2_candidates, key=lambda c: abs(c.page - midpoint))

    for candidate in ranked:
        cut_page = candidate.page - 1  # Cut *before* the H2 start page
        left_len = cut_page - shard_start
        right_len = shard_end - cut_page
        if left_len < min_pages or right_len < min_pages:
            continue
        if left_len > max_pages or right_len > max_pages:
            continue
        # VLM verification
        if not verify_section_start(page=candidate.page, title=candidate.title, ctx=ctx):
            logger.info(
                "[h2_refine] VLM rejected H2 cut at page {} ('{}')",
                candidate.page, candidate.title[:30],
            )
            continue
        logger.info(
            "[h2_refine] accepted H2 cut at page {} ('{}'), left={} right={}",
            candidate.page, candidate.title[:30], left_len, right_len,
        )
        return (
            cut_page,
            "h1_boundary",
            f"H2 refine: '{candidate.title[:60]}' at page {candidate.page}",
            candidate.confidence * 0.9,  # Slightly lower confidence than H1
        )

    return None


def _refine_with_h2(
    cuts: list[tuple[int, str, str, float]],
    page_count: int,
    min_pages: int,
    max_pages: int,
    ctx: ToolContext,
    h1_candidates: list[H1Candidate],
) -> list[tuple[int, str, str, float]]:
    """Post-process cuts: split any shard that exceeds max_pages using H2 boundaries."""
    if not ctx.blackboard.toc_hierarchies:
        return cuts

    refined: list[tuple[int, str, str, float]] = []
    previous = 0

    # Build endpoints: each cut + the implicit final boundary
    endpoints = [(cp, at, ev, cf) for cp, at, ev, cf in cuts] + [
        (page_count, "final", "", 1.0)
    ]

    for cut_page, anchor_type, evidence, confidence in endpoints:
        shard_length = cut_page - previous
        if shard_length > max_pages:
            # Try H2 refinement for this oversized shard
            h1_title = _find_h1_for_range(h1_candidates, previous, cut_page)
            h2_cut_found = False
            if h1_title:
                h2_titles = extract_children_titles(
                    ctx.blackboard.toc_hierarchies, h1_title,
                )
                if h2_titles:
                    search_pages = list(range(previous + 1, cut_page + 1))
                    page_texts = read_page_texts(
                        ctx.pdf_path, search_pages, timeout=120,
                    )
                    h2_candidates, _ = grep_titles_in_pages(
                        h2_titles, search_pages, page_texts,
                        source="h2_refine",
                    )
                    best = _pick_and_verify_best_cut(
                        h2_candidates, previous, cut_page,
                        min_pages, max_pages, ctx,
                    )
                    if best:
                        refined.append(best)
                        h2_cut_found = True
                        logger.info(
                            "[h2_refine] split oversized shard [{}-{}] at page {}",
                            previous + 1, cut_page, best[0],
                        )
                    else:
                        logger.warning(
                            "[h2_refine] no valid H2 cut for shard [{}-{}]",
                            previous + 1, cut_page,
                        )
                else:
                    logger.info(
                        "[h2_refine] no H2 titles found under H1 '{}' for shard [{}-{}]",
                        h1_title[:30], previous + 1, cut_page,
                    )
            else:
                logger.info(
                    "[h2_refine] no H1 found for oversized shard [{}-{}]",
                    previous + 1, cut_page,
                )

            # Ultimate fallback: forced_max_size
            if not h2_cut_found:
                fallback_page = previous + max_pages
                if fallback_page < cut_page:
                    refined.append((
                        fallback_page, "forced_max_size",
                        "H2 refine fallback: forced max size", 0.2,
                    ))
                    logger.warning(
                        "[h2_refine] forced_max_size fallback at page {} for shard [{}-{}]",
                        fallback_page, previous + 1, cut_page,
                    )

        # Append the original cut (skip the synthetic "final" endpoint)
        if anchor_type != "final":
            refined.append((cut_page, anchor_type, evidence, confidence))
        previous = cut_page

    return refined


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

    # C3: H2 refinement – split any shard that still exceeds max_pages
    if cuts:
        cuts = _refine_with_h2(
            cuts, page_count, min_pages, max_pages, ctx, h1_candidates,
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
