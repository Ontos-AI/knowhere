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
from app.services.document_agent.registry import has_doc_stats, has_toc_result, register_tool
from app.services.document_agent.validators import single_shard_plan, validate_shard_plan
from loguru import logger
from shared.utils.token_estimate import estimate_tokens


def derive_leaf_cut_pages(
    toc_hierarchies: list[dict[str, Any]] | None,
    *,
    offset_override: int | None = None,
) -> list[int]:
    """Derive physical page numbers of TOC leaf nodes for shard splitting.

    Leaf nodes are entries in toc_with_level whose next sibling has level <= theirs
    (i.e. they have no children). The offset from printed page to physical page is
    either provided via offset_override (VLM-calibrated) or computed arithmetically
    from toc_range and the first entry's page_number as a fallback.
    """
    if not toc_hierarchies:
        return []

    all_pages: list[int] = []
    for hier in toc_hierarchies:
        if hier.get("toc_range_unit") != "page":
            continue
        toc_range = hier.get("toc_range")
        entries = hier.get("toc_with_level")
        if not toc_range or not entries:
            continue
        if isinstance(entries, str):
            entries = _parse_toc_with_level_entries(entries)
        if not entries:
            continue

        if offset_override is not None:
            offset = offset_override
        else:
            toc_end_page = toc_range[1] if isinstance(toc_range, list) else toc_range
            first_printed = next(
                (e.get("page_number") for e in entries if e.get("page_number") is not None),
                None,
            )
            if first_printed is None:
                continue
            offset = (toc_end_page + 1) - first_printed
            logger.warning(
                "[propose_shard_plan] using arithmetic offset fallback: "
                "toc_end={} first_printed={} offset={}",
                toc_end_page,
                first_printed,
                offset,
            )

        for i, entry in enumerate(entries):
            pn = entry.get("page_number")
            if pn is None:
                continue
            is_leaf = (
                i == len(entries) - 1
                or entries[i + 1].get("level", 1) <= entry.get("level", 1)
            )
            if is_leaf:
                all_pages.append(pn + offset)

    return sorted(set(all_pages))


def split_toc_for_shard(
    toc_hierarchies: list[dict[str, Any]] | None,
    shard_page_start: int,
    shard_page_end: int,
    *,
    offset_override: int | None = None,
) -> list[dict[str, Any]] | None:
    """Build per-shard toc_hierarchies filtered to the shard's page range.

    For continuation shards (not starting at page 1), the ancestor chain of
    the first entry is prepended so downstream heading prediction has the
    full structural context.
    """
    if not toc_hierarchies:
        return None

    result: list[dict[str, Any]] = []
    for hier in toc_hierarchies:
        if hier.get("toc_range_unit") != "page":
            result.append(hier)
            continue
        toc_range = hier.get("toc_range")
        entries = hier.get("toc_with_level")
        if not toc_range or not entries:
            continue
        if isinstance(entries, str):
            entries = _parse_toc_with_level_entries(entries)
        if not entries:
            continue

        if offset_override is not None:
            offset = offset_override
        else:
            toc_end_page = toc_range[1] if isinstance(toc_range, list) else toc_range
            first_printed = next(
                (e.get("page_number") for e in entries if e.get("page_number") is not None),
                None,
            )
            if first_printed is None:
                continue
            offset = (toc_end_page + 1) - first_printed

        shard_entries: list[dict[str, Any]] = []
        first_idx: int | None = None
        for idx, entry in enumerate(entries):
            pn = entry.get("page_number")
            if pn is None:
                continue
            physical = pn + offset
            if shard_page_start <= physical <= shard_page_end:
                if first_idx is None:
                    first_idx = idx
                shard_entries.append(entry)

        if not shard_entries or first_idx is None:
            continue

        # Prepend ancestor chain for continuation shards. Walk forward through
        # every entry preceding the shard's first entry, maintaining a
        # monotonic stack of "open" ancestors: an incoming entry closes out
        # (pops) any stack entries at the same or deeper level before being
        # pushed itself. A final pop against first_entry_level removes a
        # trailing sibling that shares the same level as the shard's first
        # entry (siblings are not ancestors). This is robust to non-monotonic
        # level sequences (e.g. [L1, L2, L1, L3]), unlike a simple
        # "smallest-unseen-level" scan.
        first_entry_level = shard_entries[0].get("level", 1)
        ancestors: list[dict[str, Any]] = []
        if first_entry_level > 1:
            stack: list[dict[str, Any]] = []
            for ancestor in entries[:first_idx]:
                ancestor_level = ancestor.get("level", 1)
                while stack and stack[-1].get("level", 1) >= ancestor_level:
                    stack.pop()
                stack.append(ancestor)
            while stack and stack[-1].get("level", 1) >= first_entry_level:
                stack.pop()
            ancestors = [
                {
                    "heading": node.get("heading"),
                    "level": node.get("level", 1),
                    "page_number": None,
                }
                for node in stack
            ]

        result.append({
            "toc_range": [shard_page_start, shard_page_end],
            "toc_range_unit": "page",
            "source": hier.get("source", "vlm_shard_split"),
            "toc_with_level": ancestors + shard_entries,
        })

    return result if result else None


def _parse_toc_with_level_entries(markdown: str) -> list[dict[str, Any]]:
    """Parse toc_with_level markdown table into list of dicts."""
    entries: list[dict[str, Any]] = []
    headers: list[str] | None = None
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        if headers is None:
            headers = [cell.lower() for cell in cells]
            continue
        row = dict(zip(headers, cells))
        level = _safe_int(row.get("level"))
        heading = row.get("heading")
        page_number = _safe_int(row.get("page_number"))
        if heading and level:
            entries.append({"heading": heading, "level": level, "page_number": page_number})
    return entries


def _safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


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
    leaf_pages: list[int],
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
        "leaf_cut_pages": leaf_pages,
        "document_profile": profile,
        "visual_evidence": visual_evidence[-3:],
        "grep_history": grep_history[-3:],
    }
    return (
        "You are a senior document parsing architect. Decide whether to split a PDF "
        "and where to split it using document-scale features and TOC leaf-node evidence.\n"
        "Rules:\n"
        "- Return strict JSON only.\n"
        "- Prefer TOC leaf-node pages as semantic boundaries, cutting at page-1 when possible.\n"
        "- Do not blindly split on every leaf node. Consider total page_count, spacing, min/max "
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
    leaf_pages: list[int],
) -> tuple[list[tuple[int, str, str, float]], str]:
    cuts: list[tuple[int, str, str, float]] = []
    previous = 0
    while page_count - previous > max_pages:
        target = previous + max_pages
        eligible = [
            page for page in leaf_pages if previous + min_pages < page <= target
        ]
        if eligible:
            chosen = max(eligible)
            cut_page = chosen - 1
            cuts.append((cut_page, "h1_boundary", f"guardrail leaf node at page {chosen}", 0.35))
            previous = cut_page
        else:
            cut_page = previous + max_pages
            cuts.append((cut_page, "forced_max_size", "no leaf node in range", 0.2))
            previous = cut_page
    return cuts, "too_large"


@register_tool(
    name="propose.shard_plan",
    description="Decide whether and where to split a long PDF using TOC leaf-node boundaries.",
    preconditions=(has_doc_stats, has_toc_result),
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

    offset_hint: int | None = None
    if ctx.blackboard.toc_hierarchies:
        from app.services.document_agent.structure.hierarchy_locator import extract_toc_nodes
        from app.services.page_memory.skeleton_extractor import calibrate_offset_via_vlm

        nodes = extract_toc_nodes(ctx.blackboard.toc_hierarchies)
        offset_hint, _ = calibrate_offset_via_vlm(
            nodes=nodes,
            toc_hierarchies=ctx.blackboard.toc_hierarchies,
            ctx=ctx,
            page_texts={},
            page_count=page_count,
        )
    ctx.blackboard.toc_page_offset = offset_hint

    leaf_pages = derive_leaf_cut_pages(ctx.blackboard.toc_hierarchies, offset_override=offset_hint)
    model = ctx.settings.get("model")
    prompt = _build_prompt(
        page_count=page_count,
        min_pages=min_pages,
        max_pages=max_pages,
        doc_stats=ctx.blackboard.doc_stats,
        page_kind_counts=ctx.blackboard.global_signals.get("page_kind_counts", {}),
        toc_pages=ctx.blackboard.toc_result.toc_pages if ctx.blackboard.toc_result else [],
        leaf_pages=leaf_pages,
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
                usage_task="document_agent.propose_shard_plan",
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
                leaf_pages=leaf_pages,
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
                leaf_pages=leaf_pages,
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
            "leaf_page_count": len(leaf_pages),
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
