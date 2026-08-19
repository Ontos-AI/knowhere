"""VLM-driven TOC anchor, boundary, and entry extraction."""

from __future__ import annotations

import gc
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from app.services.document_agent.manifest import (
    TocAnchorPage,
    TocEvidence,
    TocResult,
    ToolContext,
    ToolResult,
)
from app.services.document_agent.registry import register_tool
from app.services.document_agent.tools.vlm_toc_extractor import (
    TOC_VLM_MAX_TOKENS,
    vlm_entries_to_toc_hierarchies,
)
from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)
from loguru import logger

# -- Constants -----------------------------------------------------------------

BOUNDARY_STEP_PAGES = 5
TOC_VLM_CONCURRENCY = 10
MAX_BOUNDARY_ROUNDS = 6
MAX_TOC_PAGES = BOUNDARY_STEP_PAGES * MAX_BOUNDARY_ROUNDS  # 30

_CONFIRM_PROMPT = (
    "You are a document structure analysis expert. "
    "Below are screenshot(s) of candidate pages extracted from a PDF. "
    "These pages contained keywords such as 'Table of Contents' / 'Contents' "
    "during a text scan.\n\n"
    "For each page, determine whether it is truly the **start page** of a "
    "Table of Contents (TOC).\n\n"
    "Criteria for a real TOC page:\n"
    "- Contains a list of section titles paired with page numbers\n"
    "- Titles are connected to page numbers via dots, ellipses, or spaces\n"
    "- Titles have a systematic numbering scheme (e.g. 1. / 1.1 / Chapter 1)\n\n"
    "NOT a TOC page:\n"
    "- Body text that casually mentions 'contents'\n"
    "- A page with only a 'Contents' heading but body text below\n\n"
    "Return strict JSON (no markdown fences):\n"
    '{"pages": [{"page": <page_number>, "is_toc_start": true/false, '
    '"reason": "brief reason"}]}'
)


# -- PyMuPDF workers (must be top-level for multiprocessing pickle) ------------


@worker
def _render_expand_window_worker(
    queue,
    pdf_path: str,
    pages: list[int],
    output_dir: str,
    dpi: int,
    anchor_page: int,
) -> None:
    """Render one Phase2 expand window in a single child process.

    Opens the PDF once and writes ``toc_a{anchor}_p{page}.png`` for each page.
    """
    import pymupdf  # type: ignore[import]

    results: list[dict[str, Any]] = []
    doc = None
    try:
        doc = pymupdf.open(pdf_path)
        mat = pymupdf.Matrix(dpi / 72.0, dpi / 72.0)
        for page_num in pages:
            idx = page_num - 1
            if not (0 <= idx < doc.page_count):
                continue
            pix = doc[idx].get_pixmap(matrix=mat)
            png_path = os.path.join(
                output_dir,
                f"toc_a{anchor_page}_p{page_num}.png",
            )
            pix.save(png_path)
            results.append({"page": page_num, "png_path": png_path})
    finally:
        try:
            if doc is not None:
                doc.close()
        except Exception:
            pass
        gc.collect()
    queue.put({"ok": True, "results": results})


# -- VLM helpers ---------------------------------------------------------------


def _iter_chunks(items: list[Any], size: int) -> list[list[Any]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return [items[i : i + size] for i in range(0, len(items), size)]


def _parse_confirm_items(raw: str) -> list[dict[str, Any]]:
    data = json.loads(raw)
    if isinstance(data, dict):
        items = data.get("pages") or data.get("results") or data.get("data") or []
        if not items and len(data) == 1:
            items = list(data.values())[0]
    elif isinstance(data, list):
        items = data
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def _evidence_from_confirm_items(
    items: list[dict[str, Any]],
) -> tuple[set[int], dict[int, TocEvidence]]:
    confirmed_pages: set[int] = set()
    evidence_by_page: dict[int, TocEvidence] = {}
    for item in items:
        if "page" not in item:
            continue
        page = int(item["page"])
        is_toc_start = bool(item.get("is_toc_start"))
        if is_toc_start:
            confirmed_pages.add(page)
        raw_confidence = item.get("confidence")
        try:
            confidence = (
                float(raw_confidence)
                if raw_confidence is not None
                else (0.95 if is_toc_start else 0.05)
            )
        except (TypeError, ValueError):
            confidence = 0.95 if is_toc_start else 0.05
        evidence_by_page[page] = TocEvidence(
            page_index=page,
            source="vlm",
            confidence=max(0.0, min(1.0, confidence)),
            reason=str(item.get("reason") or ""),
        )
    return confirmed_pages, evidence_by_page


def _confirm_anchor_chunk(
    chunk: list[TocAnchorPage],
    *,
    model: str,
) -> tuple[set[int], dict[int, TocEvidence], bool]:
    """Confirm one BOUNDARY_STEP_PAGES-sized anchor chunk.

    Returns ``(confirmed_pages, evidence_by_page, failed)``.
    """
    import base64

    from shared.services.ai.llm_overrides import get_vision_client

    if not chunk:
        return set(), {}, False

    content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": _CONFIRM_PROMPT},
    ]
    for anchor in chunk:
        with open(anchor.png_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        content_parts.append(
            {"type": "text", "text": f"\n--- Page {anchor.page} ---"}
        )
        content_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"},
            }
        )

    messages = cast(Any, [{"role": "user", "content": content_parts}])

    try:
        client, resolved_model = get_vision_client(requested_model=model)
        resolved = resolved_model or model
        raw, _usage = client.chat_completion_with_usage(
            messages=messages,
            model=resolved,
            temperature=0.1,
            max_tokens=TOC_VLM_MAX_TOKENS,
            response_format={"type": "json_object"},
            usage_task="document_agent.toc_anchor_confirm",
        )
        confirmed_pages, evidence_by_page = _evidence_from_confirm_items(
            _parse_confirm_items(raw)
        )
        return confirmed_pages, evidence_by_page, False
    except Exception as exc:
        logger.warning(
            "[extract.toc] VLM confirm chunk failed pages={}: {}",
            [a.page for a in chunk],
            exc,
        )
        return set(), {}, True


def _vlm_confirm_anchors(
    anchor_pages: list[TocAnchorPage],
    model: str,
) -> tuple[list[TocAnchorPage], bool, list[TocEvidence]]:
    """Phase 1: confirm TOC starts in BOUNDARY_STEP_PAGES batches (concurrent)."""
    if not anchor_pages:
        return [], False, []

    from gevent.pool import Pool as GeventPool

    chunks = _iter_chunks(anchor_pages, BOUNDARY_STEP_PAGES)
    logger.info(
        "[extract.toc] Phase 1 confirm: {} anchors → {} chunks (size={}, concurrency={})",
        len(anchor_pages),
        len(chunks),
        BOUNDARY_STEP_PAGES,
        min(TOC_VLM_CONCURRENCY, len(chunks)),
    )

    pool = GeventPool(size=min(TOC_VLM_CONCURRENCY, len(chunks)))
    jobs = [
        pool.spawn(_confirm_anchor_chunk, chunk, model=model)
        for chunk in chunks
    ]
    pool.join()

    confirmed_pages: set[int] = set()
    evidence_by_page: dict[int, TocEvidence] = {}
    chunk_failures = 0
    chunk_successes = 0
    for job in jobs:
        try:
            chunk_confirmed, chunk_evidence, failed = job.get()
        except Exception as exc:
            chunk_failures += 1
            logger.warning("[extract.toc] confirm greenlet failed: {}", exc)
            continue
        if failed:
            chunk_failures += 1
            continue
        chunk_successes += 1
        confirmed_pages.update(chunk_confirmed)
        evidence_by_page.update(chunk_evidence)

    confirm_failed = chunk_successes == 0 and chunk_failures > 0
    confirmed = [a for a in anchor_pages if a.page in confirmed_pages]
    rejected = [a.page for a in anchor_pages if a.page not in confirmed_pages]
    evidence = [
        evidence_by_page.get(
            a.page,
            TocEvidence(
                page_index=a.page,
                source="vlm",
                confidence=0.05,
                reason=(
                    "confirm batch failed for this candidate"
                    if confirm_failed
                    else "VLM response omitted this candidate page"
                ),
            ),
        )
        for a in anchor_pages
    ]
    logger.info(
        "[extract.toc] Phase 1 done: confirmed={} rejected={} "
        "chunk_ok={} chunk_fail={} confirm_failed={}",
        len(confirmed),
        rejected,
        chunk_successes,
        chunk_failures,
        confirm_failed,
    )
    return confirmed, confirm_failed, evidence


# -- Phase 2: expand + extract per confirmed start -----------------------------


@dataclass
class _TocRegionResult:
    anchor_page: int
    entries: list[dict[str, Any]] = field(default_factory=list)
    toc_pages: list[int] = field(default_factory=list)
    hierarchies: list[dict[str, Any]] = field(default_factory=list)
    batch_meta: list[dict[str, Any]] = field(default_factory=list)
    batch_trace: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def _render_toc_page_batch(
    *,
    pdf_path: str,
    output_dir: str,
    dpi: int,
    anchor_page: int,
    batch_pages: list[int],
    render_lock: Any,
    reuse_png_by_page: dict[int, str] | None = None,
) -> list[tuple[int, str]]:
    """Render one expand window under the Phase2 global render lock.

    One child process renders the whole window (PDF opened once). Across
    anchors, PyMuPDF stays serial to avoid gevent ThreadPool deadlocks.
    VLM calls happen outside this lock.
    """
    reuse = reuse_png_by_page or {}
    planned: dict[int, str] = {}
    pages_to_render: list[int] = []
    for page_num in batch_pages:
        existing = reuse.get(page_num)
        if existing and os.path.isfile(existing):
            planned[page_num] = existing
        else:
            pages_to_render.append(page_num)

    with render_lock:
        if pages_to_render:
            result = run_in_child_process(
                _render_expand_window_worker,
                pdf_path,
                pages_to_render,
                output_dir,
                dpi,
                anchor_page,
                timeout=120,
            )
            for item in result.get("results") or []:
                planned[int(item["page"])] = str(item["png_path"])

    page_pngs: list[tuple[int, str]] = []
    missing: list[int] = []
    for page_num in batch_pages:
        png_path = planned.get(page_num)
        if not png_path:
            missing.append(page_num)
            continue
        page_pngs.append((page_num, png_path))
    if missing:
        raise RuntimeError(
            f"TOC expand render missing pages {missing} for anchor {anchor_page}"
        )
    return page_pngs


def _extract_region_for_anchor(
    anchor: TocAnchorPage,
    *,
    pdf_path: str,
    page_count: int,
    output_dir: str,
    dpi: int,
    model: str,
    render_lock: Any,
) -> _TocRegionResult:
    """Expand + extract for one confirmed TOC start.

    Rounds within a start stay serial (continuation context). Different
    starts run concurrently for VLM, but page renders share ``render_lock``.
    """
    from app.services.document_agent.tools.vlm_toc_extractor import (
        vlm_extract_toc_batch,
    )

    anchor_page = anchor.page
    region_entries: list[dict[str, Any]] = []
    region_toc_pages: list[int] = []
    region_scan_end = anchor_page
    batch_meta: list[dict[str, Any]] = []
    batch_trace: list[dict[str, Any]] = []

    try:
        for round_idx in range(MAX_BOUNDARY_ROUNDS):
            batch_start = anchor_page + round_idx * BOUNDARY_STEP_PAGES
            batch_end = min(batch_start + BOUNDARY_STEP_PAGES - 1, page_count)
            if batch_start > page_count:
                break

            batch_pages = list(range(batch_start, batch_end + 1))
            logger.info(
                "[extract.toc] batch round {}: pages {}-{} for anchor {}",
                round_idx,
                batch_start,
                batch_end,
                anchor_page,
            )

            reuse_png_by_page: dict[int, str] = {}
            if (
                round_idx == 0
                and anchor.png_path
                and os.path.isfile(anchor.png_path)
            ):
                # Phase1 already rendered the confirmed start page.
                reuse_png_by_page[anchor_page] = anchor.png_path

            page_pngs = _render_toc_page_batch(
                pdf_path=pdf_path,
                output_dir=output_dir,
                dpi=dpi,
                anchor_page=anchor_page,
                batch_pages=batch_pages,
                render_lock=render_lock,
                reuse_png_by_page=reuse_png_by_page,
            )

            batch_result = vlm_extract_toc_batch(
                page_pngs=page_pngs,
                model=model,
                previous_entries=region_entries if region_entries else None,
            )
            batch_meta.append(batch_result.meta)
            region_entries.extend(batch_result.all_entries)
            region_toc_pages.extend(batch_result.toc_pages)
            region_scan_end = batch_end
            batch_trace.append(
                {
                    "anchor": anchor_page,
                    "round": round_idx,
                    "batch_pages": batch_pages,
                    "toc_pages": batch_result.toc_pages,
                    "non_toc_pages": batch_result.non_toc_pages,
                    "entries_found": len(batch_result.all_entries),
                }
            )

            last_page_is_toc = (
                batch_result.page_results
                and batch_result.page_results[-1].is_toc
            )
            if not last_page_is_toc:
                logger.info(
                    "[extract.toc] boundary found: last page {} is not TOC",
                    batch_end,
                )
                break
            if batch_end >= page_count:
                break
            logger.info(
                "[extract.toc] last page {} still TOC, expanding window",
                batch_end,
            )

        hierarchies: list[dict[str, Any]] = []
        if region_entries:
            hierarchies = vlm_entries_to_toc_hierarchies(
                region_entries,
                toc_page_nums=region_toc_pages,
                scan_end_page=region_scan_end,
                page_count=page_count,
            )
        return _TocRegionResult(
            anchor_page=anchor_page,
            entries=region_entries,
            toc_pages=region_toc_pages,
            hierarchies=hierarchies,
            batch_meta=batch_meta,
            batch_trace=batch_trace,
        )
    except Exception as exc:
        logger.warning(
            "[extract.toc] anchor {} region extract failed: {}",
            anchor_page,
            exc,
        )
        return _TocRegionResult(
            anchor_page=anchor_page,
            entries=region_entries,
            toc_pages=region_toc_pages,
            batch_meta=batch_meta,
            batch_trace=batch_trace,
            error=str(exc),
        )


def _extract_regions_for_confirmed_anchors(
    confirmed: list[TocAnchorPage],
    *,
    pdf_path: str,
    page_count: int,
    output_dir: str,
    dpi: int,
    model: str,
) -> list[_TocRegionResult]:
    """Phase 2: concurrent VLM per start; serial PyMuPDF renders across starts."""
    if not confirmed:
        return []

    from gevent.lock import Semaphore
    from gevent.pool import Pool as GeventPool

    pool_size = min(TOC_VLM_CONCURRENCY, len(confirmed))
    render_lock = Semaphore(1)
    logger.info(
        "[extract.toc] Phase 2 extract: {} confirmed starts, "
        "vlm_concurrency={}, render=serial",
        len(confirmed),
        pool_size,
    )
    pool = GeventPool(size=pool_size)
    jobs = [
        pool.spawn(
            _extract_region_for_anchor,
            anchor,
            pdf_path=pdf_path,
            page_count=page_count,
            output_dir=output_dir,
            dpi=dpi,
            model=model,
            render_lock=render_lock,
        )
        for anchor in confirmed
    ]
    pool.join()

    by_anchor: dict[int, _TocRegionResult] = {}
    for job in jobs:
        try:
            result = job.get()
        except Exception as exc:
            logger.warning("[extract.toc] region greenlet failed: {}", exc)
            continue
        by_anchor[result.anchor_page] = result

    # Preserve document page order when merging regions.
    ordered: list[_TocRegionResult] = []
    for anchor in confirmed:
        result = by_anchor.get(anchor.page)
        if result is None:
            ordered.append(
                _TocRegionResult(
                    anchor_page=anchor.page,
                    error="region greenlet failed",
                )
            )
        else:
            ordered.append(result)
    return ordered


# -- Main tool -----------------------------------------------------------------


@register_tool(
    name="extract.toc_with_boundaries",
    description=(
        "VLM-confirms TOC anchor pages, then batch-classifies and extracts "
        "TOC entries from rendered page windows using VLM."
    ),
)
def extract_toc_with_boundaries(
    ctx: ToolContext, _args: dict[str, Any]
) -> ToolResult:
    start = time.monotonic()
    anchors = ctx.blackboard.toc_anchor_pages
    warnings: list[str] = []
    debug_info: dict[str, Any] = {}

    if not anchors:
        logger.info("[extract.toc] no anchor pages, skipping")
        ctx.blackboard.toc_result = TocResult(
            method="none",
            notes="No TOC anchor pages found by find.toc_anchor_pages",
            failure_kind="none",
        )
        return ToolResult(
            status="ok",
            payload={"toc_count": 0},
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    model = ctx.settings.get("vlm_model") or os.environ.get("IMAGE_MODEL")
    if not model:
        logger.warning("[extract.toc] no VLM model configured; skipping TOC extraction")
        ctx.blackboard.toc_result = TocResult(
            method="none",
            notes="No VLM model configured for TOC extraction",
            failure_kind="degraded",
        )
        return ToolResult(
            status="ok",
            payload={"toc_count": 0},
            latency_ms=int((time.monotonic() - start) * 1000),
            warnings=["No VLM model configured; skipping TOC extraction."],
        )
    dpi = int(ctx.settings.get("toc_png_dpi", "144"))
    page_count = ctx.blackboard.page_count
    output_dir = str(
        Path(ctx.output_dir or os.path.expanduser("~/.knowhere/_debug_profile"))
        / "toc_pages"
    )
    os.makedirs(output_dir, exist_ok=True)

    # -- Phase 1: VLM confirm anchors (batched + concurrent) -------------------
    confirmed, confirm_failed, confirm_evidence = _vlm_confirm_anchors(anchors, model)
    if confirm_failed:
        warnings.append("vlm_anchor_confirmation_failed")
    debug_info["phase1_confirmed"] = [a.page for a in confirmed]
    debug_info["phase1_rejected"] = (
        [] if confirm_failed else [a.page for a in anchors if a not in confirmed]
    )
    if confirm_failed:
        debug_info["phase1_unconfirmed"] = [a.page for a in anchors]

    if not confirmed:
        if confirm_failed:
            ctx.blackboard.toc_result = TocResult(
                candidates=list(anchors),
                evidence=confirm_evidence,
                method="none",
                notes="VLM anchor confirmation failed; TOC candidates left unconfirmed",
                failure_kind="confirm_failed",
            )
            return ToolResult(
                status="ok",
                payload={"toc_count": 0},
                latency_ms=int((time.monotonic() - start) * 1000),
                warnings=warnings,
                debug=debug_info,
            )
        ctx.blackboard.toc_result = TocResult(
            candidates=list(anchors),
            evidence=confirm_evidence,
            method="none",
            notes="VLM rejected all TOC anchor candidates",
            failure_kind="rejected_all",
        )
        return ToolResult(
            status="ok",
            payload={"toc_count": 0},
            latency_ms=int((time.monotonic() - start) * 1000),
            warnings=["VLM rejected all anchor pages"],
            debug=debug_info,
        )

    # -- Phase 2: per-confirmed-start expand + extract (concurrent across starts)
    region_results = _extract_regions_for_confirmed_anchors(
        confirmed,
        pdf_path=ctx.pdf_path,
        page_count=page_count,
        output_dir=output_dir,
        dpi=dpi,
        model=model,
    )

    all_entries: list[dict[str, Any]] = []
    all_toc_pages: list[int] = []
    toc_hierarchies: list[dict[str, Any]] = []
    batch_meta: list[dict[str, Any]] = []
    batch_trace: list[dict[str, Any]] = []

    for region in region_results:
        if region.error:
            warnings.append(
                f"toc_region_failed:anchor={region.anchor_page}:{region.error}"
            )
            logger.warning(
                "[extract.toc] anchor {} region failed: {}",
                region.anchor_page,
                region.error,
            )
            continue
        all_entries.extend(region.entries)
        all_toc_pages.extend(region.toc_pages)
        batch_meta.extend(region.batch_meta)
        batch_trace.extend(region.batch_trace)
        if region.hierarchies:
            toc_hierarchies.extend(region.hierarchies)
        elif not region.entries:
            logger.warning(
                "[extract.toc] anchor {} produced no TOC entries",
                region.anchor_page,
            )

    if not all_entries:
        raise RuntimeError(
            "VLM TOC extractor returned no entries for confirmed TOC pages"
        )

    debug_info["batch_trace"] = batch_trace
    debug_info["batch_meta"] = batch_meta
    debug_info["vlm_entry_count"] = len(all_entries)

    all_toc_pages_sorted = sorted(set(all_toc_pages))
    toc_region_count = len(toc_hierarchies)

    ctx.blackboard.toc_result = TocResult(
        toc_pages=all_toc_pages_sorted,
        evidence=confirm_evidence,
        method="vlm_batch",
        notes=(
            f"VLM confirmed {len(confirmed)} TOC starts, "
            f"batch classify+extract found {toc_region_count} regions, "
            f"toc_pages={all_toc_pages_sorted}"
        ),
        failure_kind="none",
    )
    ctx.blackboard.toc_hierarchies = toc_hierarchies if toc_hierarchies else None

    # Build toc_ranges from confirmed TOC pages for summary
    toc_ranges_out: list[list[int]] = []
    if toc_hierarchies:
        for hier in toc_hierarchies:
            toc_ranges_out.append(hier.get("toc_range", []))

    toc_summary: dict[str, Any] = {
        "toc_ranges": toc_ranges_out,
        "toc_page_count": len(all_toc_pages_sorted),
        "toc_entry_count": len(all_entries),
        "toc_region_count": toc_region_count,
        "toc_source": "vlm_batch",
    }
    if toc_hierarchies:
        for i, hier in enumerate(toc_hierarchies):
            tree = hier.get("toc_tree", {})
            toc_summary[f"region_{i}_level1_count"] = len(tree)
            toc_summary[f"region_{i}_level1_titles"] = list(tree.keys())[:10]

    return ToolResult(
        status="ok",
        payload={
            "toc_count": len(toc_hierarchies) if toc_hierarchies else 0,
            "toc_page_count": len(all_toc_pages_sorted),
            "toc_region_count": toc_region_count,
        },
        latency_ms=int((time.monotonic() - start) * 1000),
        output_summary=toc_summary,
        warnings=warnings,
        debug=debug_info,
    )
