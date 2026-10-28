"""Deterministic calibration Phase-1: regime partition + forward-scan offsets.

Entries are partitioned by printed-label kind (the same classifier Phase-2
uses). Each regime takes leaf probes from its first few distinct printed pages
and scans forward until one is confirmed; that single confirmation fixes the
regime's candidate offset. Phase-2 owns tail verification and bulk anchoring.

A probe scans from the monotonic cursor ``scan_floor`` (initially
``toc_range end + 1``, body after the TOC). When an earlier regime confirms an
offset, ``scan_floor`` advances past the physical span of that regime's printed
entries so subsequent regimes (e.g. decimal body after roman front-matter) do
not burn their scan window inside the earlier regime. Within one regime, the
initial scan starts after the TOC, allowing negative ``physical - printed``
offsets (e.g. journal reprints whose printed labels exceed the PDF length).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from app.services.document_agent.calibration.scan import (
    TitleScanResult,
    scan_title_forward,
)
from app.services.document_agent.calibration.types import (
    FAILURE_NO_OFFSET,
    FAILURE_PAGE_COUNT_MISSING,
    FAILURE_TOC_EMPTY,
    CalibrationRegime,
    CalibrationResult,
    CalibrationSample,
)
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.structure.anchoring_primitives import toc_range_end
from app.services.document_agent.structure.hierarchy_locator import (
    classify_page_number_kind,
    parse_printed_page,
)

PROBES_PER_REGIME = 2


@dataclass(frozen=True)
class _Probe:
    title: str
    printed: int


@dataclass
class _RegimeBucket:
    kind: str
    probes: list[_Probe] = field(default_factory=list)
    printed_pages: list[int] = field(default_factory=list)


def _region_entries(
    hierarchies: list[dict[str, Any]],
    region_index: int,
) -> list[dict[str, Any]]:
    if region_index < 0 or region_index >= len(hierarchies):
        raise IndexError(f"region_index out of range: {region_index}")
    region = hierarchies[region_index]
    entries = region.get("toc_with_level") if isinstance(region, dict) else None
    return [entry for entry in (entries or []) if isinstance(entry, dict)]


def _regime_buckets(
    entries: list[dict[str, Any]],
    *,
    limit: int,
) -> dict[str, _RegimeBucket]:
    """Collect leaf probes and all parsed printed pages per regime in TOC order."""
    from app.services.document_parser.structure.body_boundary import (
        normalize_heading_label,
    )

    buckets: dict[str, _RegimeBucket] = {}
    seen_printed: dict[str, set[int]] = {}
    levels = [int(entry.get("level") or 1) for entry in entries]
    for index, entry in enumerate(entries):
        label = entry.get("page_number")
        kind = classify_page_number_kind(label)
        printed = parse_printed_page(label, kind=kind)
        if printed is None:
            continue
        bucket = buckets.get(kind)
        if bucket is None:
            bucket = _RegimeBucket(kind=kind)
            buckets[kind] = bucket
        bucket.printed_pages.append(printed)
        if index + 1 < len(entries) and levels[index + 1] > levels[index]:
            continue
        if len(bucket.probes) >= limit:
            continue
        if printed in seen_printed.get(kind, set()):
            continue
        title = normalize_heading_label(str(entry.get("heading") or ""))
        if not title:
            continue
        bucket.probes.append(_Probe(title=title, printed=printed))
        seen_printed.setdefault(kind, set()).add(printed)
    return {kind: b for kind, b in buckets.items() if b.probes}


def _next_regime_scan_floor(
    *,
    current_floor: int,
    bucket: _RegimeBucket,
    offset: int,
    found_page: int,
    page_count: int,
) -> int:
    in_bounds_physical = [
        printed + offset
        for printed in bucket.printed_pages
        if 1 <= printed + offset <= page_count
    ]
    regime_last_physical = max([found_page, *in_bounds_physical])
    next_floor = max(current_floor, regime_last_physical + 1)
    return next_floor if next_floor <= page_count else current_floor


def run_calibration_phase1(
    *,
    ctx: ToolContext,
    toc_hierarchies: list[dict[str, Any]],
    region_index: int = 0,
    page_count: int | None = None,
    probes_per_regime: int = PROBES_PER_REGIME,
) -> CalibrationResult:
    """Find one candidate offset per page-numbering regime in this TOC region."""
    hierarchies = list(toc_hierarchies or [])
    if not hierarchies:
        return CalibrationResult(
            status="failed",
            notes="toc_hierarchies empty",
            failure_kind=FAILURE_TOC_EMPTY,
            region_index=region_index,
        )

    resolved_page_count = int(page_count or ctx.blackboard.page_count or 0)
    if not resolved_page_count:
        return CalibrationResult(
            status="failed",
            notes="page_count unknown",
            failure_kind=FAILURE_PAGE_COUNT_MISSING,
            region_index=region_index,
        )
    ctx.blackboard.page_count = resolved_page_count

    region = hierarchies[region_index]
    toc_end = toc_range_end(region) if isinstance(region, dict) else None
    if toc_end is None:
        return CalibrationResult(
            status="failed",
            notes="toc_range end unknown",
            failure_kind=FAILURE_TOC_EMPTY,
            region_index=region_index,
        )
    region_scan_start = toc_end + 1
    if region_scan_start > resolved_page_count:
        return CalibrationResult(
            status="failed",
            notes=(
                f"scan start {region_scan_start} beyond "
                f"page_count {resolved_page_count}"
            ),
            failure_kind=FAILURE_NO_OFFSET,
            region_index=region_index,
        )

    buckets = _regime_buckets(
        _region_entries(hierarchies, region_index), limit=probes_per_regime
    )
    regimes: list[CalibrationRegime] = []
    scans: list[TitleScanResult] = []
    scan_floor = region_scan_start

    for kind, bucket in buckets.items():
        for probe in bucket.probes:
            scan = scan_title_forward(
                ctx=ctx,
                title=probe.title,
                start_page=scan_floor,
                page_count=resolved_page_count,
            )
            scans.append(scan)
            if not scan.found or scan.found_page is None:
                continue
            offset = scan.found_page - probe.printed
            regimes.append(
                CalibrationRegime(
                    kind=kind,
                    offset=offset,
                    samples=[
                        CalibrationSample(title=probe.title, physical=scan.found_page)
                    ],
                )
            )
            scan_floor = _next_regime_scan_floor(
                current_floor=scan_floor,
                bucket=bucket,
                offset=offset,
                found_page=scan.found_page,
                page_count=resolved_page_count,
            )
            break

    inspect_calls = sum(len(scan.scanned_pages) for scan in scans)
    logger.info(
        "[calibration.phase1] region={} regimes={} offsets={} inspect_calls={}",
        region_index,
        len(buckets),
        [regime.offset for regime in regimes],
        inspect_calls,
    )
    if not regimes:
        return CalibrationResult(
            status="failed",
            notes=f"no regime confirmed after {inspect_calls} inspect call(s)",
            failure_kind=FAILURE_NO_OFFSET,
            region_index=region_index,
            tool_calls=inspect_calls,
            scans=[scan.to_dict() for scan in scans],
        )
    return CalibrationResult(
        status="ok",
        regimes=regimes,
        notes=f"{len(regimes)}/{len(buckets)} regime(s) confirmed by forward scan",
        region_index=region_index,
        tool_calls=inspect_calls,
        scans=[scan.to_dict() for scan in scans],
    )
