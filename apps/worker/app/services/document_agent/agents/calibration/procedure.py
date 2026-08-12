"""Phase-2 completion aligned with production anchoring.

After the agent submits candidate regime offsets, this module:
1. Picks the primary (usually decimal) candidate offset
2. Builds TitleNodes the same way production does (``extract_toc_nodes``)
3. Runs ``anchor_hierarchy_from_offset`` (prune → bulk/bisect → null-page)

The returned ``SkeletonAnchor`` is the production schema swap point.
Regime metadata is retained only as experiment diagnostics.
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from app.services.document_agent.agents.calibration.types import (
    CalibrationRegime,
    CalibrationResult,
    CalibrationSegment,
)
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.hierarchy_locator import (
    TitleMatch,
    extract_toc_nodes,
    iter_leaf_title_nodes,
)
from app.services.document_agent.structure.structure_anchoring import (
    SkeletonAnchor,
    anchor_hierarchy_from_offset,
    serialize_skeleton_anchor,
)


_ROMAN_MAP = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def classify_page_number_kind(label: Any) -> str:
    text = str(label or "").strip()
    if not text:
        return "other"
    if re.fullmatch(r"\d+", text):
        return "decimal"
    if re.fullmatch(r"[ivxlcdm]+", text, flags=re.IGNORECASE):
        return "roman"
    if re.fullmatch(r"[A-Za-z]+-\d+", text):
        return "prefixed"
    return "other"


def parse_printed_page(label: Any, *, kind: str) -> int | None:
    text = str(label or "").strip()
    if not text:
        return None
    kind_l = (kind or "").lower()
    if kind_l in {"decimal", "arabic", "arabic_digits"}:
        return int(text) if text.isdigit() else None
    if kind_l == "roman":
        return _roman_to_int(text)
    if kind_l in {"prefixed", "folio"}:
        match = re.fullmatch(r"[A-Za-z]+-(\d+)", text)
        return int(match.group(1)) if match else None
    if text.isdigit():
        return int(text)
    if re.fullmatch(r"[ivxlcdm]+", text, flags=re.IGNORECASE):
        return _roman_to_int(text)
    match = re.fullmatch(r"[A-Za-z]+-(\d+)", text)
    return int(match.group(1)) if match else None


def _roman_to_int(text: str) -> int | None:
    raw = text.strip().lower()
    if not raw or not re.fullmatch(r"[ivxlcdm]+", raw):
        return None
    total = 0
    prev = 0
    for ch in reversed(raw):
        value = _ROMAN_MAP.get(ch)
        if value is None:
            return None
        if value < prev:
            total -= value
        else:
            total += value
            prev = value
    return total if total > 0 else None


def normalize_kind(kind: str) -> str:
    text = (kind or "other").strip().lower()
    if text in {"arabic", "arabic_digits", "decimal"}:
        return "decimal"
    if text == "roman":
        return "roman"
    if text in {"prefixed", "folio"}:
        return "prefixed"
    return text or "other"


def pick_primary_offset(result: CalibrationResult) -> int | None:
    """Prefer decimal-regime candidate offset; else first regime with an offset."""
    for regime in result.regimes:
        if normalize_kind(regime.kind) == "decimal" and regime.offset is not None:
            return int(regime.offset)
    for regime in result.regimes:
        if regime.offset is not None:
            return int(regime.offset)
    if result.offset is not None:
        return int(result.offset)
    return None


def _seed_overrides_from_samples(
    *,
    result: CalibrationResult,
    nodes: list[Any],
) -> dict[tuple[str, ...], TitleMatch]:
    """Map Phase-1 confirmed samples onto leaf paths when titles match."""
    title_to_path: dict[str, tuple[str, ...]] = {}
    for path, node in iter_leaf_title_nodes(nodes):
        title_to_path[node.title] = path

    overrides: dict[tuple[str, ...], TitleMatch] = {}
    for regime in result.regimes:
        for sample in regime.samples:
            if sample.physical is None or not sample.title:
                continue
            path = title_to_path.get(sample.title.strip())
            if path is None:
                # Soft match: normalized equality
                needle = sample.title.strip().lower()
                for title, candidate in title_to_path.items():
                    if title.lower() == needle:
                        path = candidate
                        break
            if path is None:
                continue
            overrides[path] = TitleMatch(
                page=int(sample.physical),
                confidence=0.85,
                source="agent_vlm",
                matched_line="",
                score=0.85,
                candidates=[int(sample.physical)],
                evidence={
                    "calibration": True,
                    "printed_label": sample.printed_label,
                    "method": sample.method or "agent_phase1",
                    "regime_kind": regime.kind,
                },
            )
    return overrides


def _annotate_regimes_from_anchor(
    *,
    result: CalibrationResult,
    anchor: SkeletonAnchor,
    entries: list[dict[str, Any]],
) -> list[CalibrationRegime]:
    """Attach production segment view onto agent regimes for diagnostics."""
    out: list[CalibrationRegime] = []
    for regime in result.regimes:
        kind = normalize_kind(regime.kind)
        indices = list(regime.entry_indices or [])
        if not indices:
            indices = [
                idx
                for idx, entry in enumerate(entries)
                if isinstance(entry, dict)
                and classify_page_number_kind(entry.get("page_number")) == kind
            ]

        # Production trees only integer-print leaves enter bulk; decimal regime
        # maps directly onto SkeletonAnchor bulk when Phase-2 succeeded.
        if (
            kind == "decimal"
            and anchor.offset is not None
            and int(anchor.bulk_count or 0) > 0
        ):
            ok_indices = indices
            no_toc: list[int] = []
            segments = [
                CalibrationSegment(
                    offset=int(anchor.offset),
                    leaf_start=0,
                    leaf_end=max(0, len(ok_indices) - 1),
                    entry_indices=ok_indices,
                    status="ok",
                )
            ]
        else:
            ok_indices = []
            no_toc = list(indices)
            segments = []

        out.append(
            CalibrationRegime(
                kind=kind,
                offset=anchor.offset if kind == "decimal" else regime.offset,
                offset_status="ok" if segments else "failed",
                entry_indices=indices,
                samples=list(regime.samples),
                posterior=list(regime.posterior),
                segments=segments,
                no_toc_entry_indices=no_toc,
                notes=(
                    f"production_bulk={anchor.bulk_count}; "
                    f"locate_agent={anchor.locate_agent}"
                ),
            )
        )
    return out


def finalize_calibration_result(
    *,
    result: CalibrationResult,
    entries: list[dict[str, Any]],
    toc_hierarchies: list[dict[str, Any]],
    ctx: ToolContext,
    page_count: int,
    page_texts: dict[int, str] | None = None,
    body_pages: list[int] | None = None,
) -> tuple[SkeletonAnchor, CalibrationResult]:
    """Run production Phase-2 from an agent candidate offset."""
    offset_hint = pick_primary_offset(result)
    texts = dict(page_texts or {})
    bodies = list(body_pages or sorted(texts.keys()) or list(range(1, page_count + 1)))
    # Same TitleNode prep as C4 / extract_section_skeletons before anchoring.
    from app.services.page_memory.skeleton_extractor import (
        _collapse_intermediate_single_child_chains,
    )

    nodes = _collapse_intermediate_single_child_chains(
        extract_toc_nodes(toc_hierarchies)
    )
    seed = _seed_overrides_from_samples(result=result, nodes=nodes)

    working, anchor = anchor_hierarchy_from_offset(
        nodes=nodes,
        offset_hint=offset_hint,
        calibration_overrides=seed,
        page_texts=texts,
        body_pages=bodies,
        page_count=page_count,
        ctx=ctx,
    )
    logger.info(
        "[calibration.completion] offset={} status={} bulk={} pruned={} nodes={}",
        anchor.offset,
        anchor.offset_status,
        anchor.bulk_count,
        anchor.pruned_count,
        len(working),
    )

    regimes = _annotate_regimes_from_anchor(
        result=result, anchor=anchor, entries=entries
    )
    complete = sum(len(r.segments) for r in regimes)
    notes_parts = [result.notes] if result.notes else []
    notes_parts.append(
        f"phase2 production locate_agent={anchor.locate_agent} "
        f"bulk={anchor.bulk_count} complete_regime_segments={complete}"
    )
    finalized = CalibrationResult(
        status="ok" if anchor.offset_status == "ok" and anchor.bulk_count > 0 else "failed",
        regimes=regimes,
        offset=anchor.offset,
        offset_status=anchor.offset_status,
        tool_calls=result.tool_calls,
        notes="; ".join(p for p in notes_parts if p),
        region_index=result.region_index,
        history_tail=list(result.history_tail),
    )
    return anchor, finalized


def build_calibration_payload(
    *,
    anchor: SkeletonAnchor,
    result: CalibrationResult,
    no_links: bool,
    region_payloads: list[dict[str, Any]] | None = None,
    tool_calls: int | None = None,
) -> dict[str, Any]:
    """Production SkeletonAnchor fields + experiment diagnostics."""
    payload = serialize_skeleton_anchor(anchor)
    payload.update(
        {
            "status": result.status,
            "regimes": [regime.to_dict() if hasattr(regime, "to_dict") else regime for regime in (
                # dataclasses asdict via CalibrationResult
                result.to_dict().get("regimes") or []
            )],
            "regions": list(region_payloads or []),
            "tool_calls": int(tool_calls if tool_calls is not None else result.tool_calls),
            "notes": result.notes,
            "no_links": no_links,
        }
    )
    return payload
