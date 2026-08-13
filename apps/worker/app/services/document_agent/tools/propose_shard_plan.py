"""Deterministic long-PDF shard planning from TOC leaf boundaries."""

from __future__ import annotations

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


def derive_leaf_cut_pages(
    toc_hierarchies: list[dict[str, Any]] | None,
    *,
    offset_override: int | None = None,
) -> list[int]:
    """Derive physical page numbers of TOC leaf nodes for shard splitting.

    Leaf nodes are entries in toc_with_level whose next sibling has level <= theirs
    (i.e. they have no children). Requires a calibrated ``offset_override``;
    without it this returns [] and the caller falls back to non-TOC planning.
    """
    if not toc_hierarchies or offset_override is None:
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

        offset = offset_override
        for i, entry in enumerate(entries):
            pn = entry.get("page_number")
            if not isinstance(pn, int):
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

    Requires calibrated ``offset_override`` for page-unit TOC regions.
    """
    if not toc_hierarchies:
        return None
    if offset_override is None:
        # Without a calibrated offset, keep non-page TOC payloads as-is and
        # skip page-unit hierarchies rather than inventing arithmetic offsets.
        kept = [
            hier
            for hier in toc_hierarchies
            if hier.get("toc_range_unit") != "page"
        ]
        return kept or None

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

        offset = offset_override

        shard_entries: list[dict[str, Any]] = []
        first_idx: int | None = None
        for idx, entry in enumerate(entries):
            pn = entry.get("page_number")
            if not isinstance(pn, int):
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


def _finest_toc_ranges(leaf_pages: list[int], page_count: int) -> list[tuple[int, int]]:
    starts = sorted({page for page in leaf_pages if 1 <= page <= page_count})
    if not starts:
        return []
    ranges: list[tuple[int, int]] = []
    if starts[0] > 1:
        ranges.append((1, starts[0] - 1))
    for index, start in enumerate(starts):
        end = starts[index + 1] - 1 if index + 1 < len(starts) else page_count
        if end >= start:
            ranges.append((start, end))
    return ranges


def _pack_range_by_blanks(
    *,
    previous: int,
    end: int,
    max_pages: int,
    blank_pages: list[int],
) -> list[tuple[int, str, str, float]]:
    cuts: list[tuple[int, str, str, float]] = []
    while end - previous > max_pages:
        target = previous + max_pages
        eligible = [
            page for page in blank_pages
            if previous + (max_pages - 20) < page <= target
        ]
        if eligible:
            chosen = max(eligible)
            cuts.append((chosen, "blank_separator", f"blank-like page at {chosen}", 0.5))
            previous = chosen
        else:
            cut_page = previous + max_pages
            cuts.append((cut_page, "forced_max_size", "no separator in range", 0.2))
            previous = cut_page
    return cuts


def _deterministic_leaf_plan(
    *,
    page_count: int,
    max_pages: int,
    leaf_pages: list[int],
    blank_pages: list[int],
) -> tuple[list[tuple[int, str, str, float]], str]:
    ranges = _finest_toc_ranges(leaf_pages, page_count)
    cuts: list[tuple[int, str, str, float]] = []
    shard_start = 0
    index = 0
    while index < len(ranges):
        start, end = ranges[index]
        if end - shard_start <= max_pages:
            index += 1
            continue
        prior_end = start - 1
        if prior_end > shard_start:
            cuts.append((prior_end, "toc_leaf_boundary", f"toc leaf at page {start}", 0.85))
            shard_start = prior_end
            continue
        range_cuts = _pack_range_by_blanks(
            previous=shard_start,
            end=end,
            max_pages=max_pages,
            blank_pages=blank_pages,
        )
        cuts.extend(range_cuts)
        if range_cuts:
            shard_start = range_cuts[-1][0]
        index += 1
    return cuts, "too_large"


def _get_blank_pages(ctx: ToolContext) -> list[int]:
    features = ctx.blackboard.page_features or []
    return sorted(feature.page for feature in features if feature.is_blank_like)


@register_tool(
    name="propose.shard_plan",
    description="Split a long PDF at TOC leaf boundaries, then blank pages, then max page size.",
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

    leaf_pages = derive_leaf_cut_pages(
        ctx.blackboard.toc_hierarchies,
        offset_override=ctx.blackboard.toc_page_offset,
    )
    blank_pages = _get_blank_pages(ctx)
    if leaf_pages:
        cuts, reason = _deterministic_leaf_plan(
            page_count=page_count,
            max_pages=max_pages,
            leaf_pages=leaf_pages,
            blank_pages=blank_pages,
        )
        rationale = "Deterministic plan from TOC leaf boundaries."
    else:
        cuts = _pack_range_by_blanks(
            previous=0,
            end=page_count,
            max_pages=max_pages,
            blank_pages=blank_pages,
        )
        reason = "too_large"
        rationale = "Deterministic plan from blank-like page boundaries (no TOC)."

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
        input_summary={
            "page_count": page_count,
            "leaf_page_count": len(leaf_pages),
        },
        output_summary={
            "enabled": plan.enabled,
            "reason": plan.reason,
            "rationale": rationale,
            "shards": [shard.to_dict() for shard in plan.shards],
        },
    )
