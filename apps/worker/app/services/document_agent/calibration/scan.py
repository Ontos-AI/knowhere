"""Deterministic forward scan for a TOC title via ``inspect.pages``.

A TOC entry gives a printed page label; the physical page is that candidate or
some page after it. The scan walks forward from the candidate with a widening
window, feeding each round's cursor into the next one, so a miss never re-opens
pages that were already inspected.

Each round still covers ``window_schedule[i]`` pages. PDF→PNG for the whole
window is rendered in one serial child-process call (same discipline as TOC
extract: never drive the gevent PyMuPDF pool from a ThreadPool). VLM inspect
then runs one page per call, concurrently.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from app.services.document_agent.calibration.prompts import (
    SECTION_START_PAGE_ANSWER_KEYS,
    build_section_start_page_question,
    coerce_found,
    coerce_reason,
)
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.tools.inspect_pages import inspect_pages
from app.services.document_agent.visual import render_pages
from shared.services.ai.token_tracking import (
    bind_token_tracker,
    get_current_token_tracker_root_id,
)

DEFAULT_WINDOW_SCHEDULE: tuple[int, ...] = (2, 4, 6, 10)


def progressive_page_windows(
    *,
    start_page: int,
    end_page: int,
    window_schedule: tuple[int, ...] = DEFAULT_WINDOW_SCHEDULE,
) -> list[list[int]]:
    """Return non-overlapping physical-page windows clipped to ``end_page``."""
    cursor = max(int(start_page), 1)
    last_page = int(end_page)
    windows: list[list[int]] = []
    for size in window_schedule:
        if cursor > last_page:
            break
        pages = list(range(cursor, min(cursor + int(size), last_page + 1)))
        if not pages:
            break
        windows.append(pages)
        cursor = pages[-1] + 1
    return windows


@dataclass
class PageInspectResult:
    page: int
    found: bool
    reason: str = ""
    error: str = ""


@dataclass
class ScanRound:
    pages: list[int]
    found: bool
    found_page: int | None = None
    error: str = ""
    page_results: list[PageInspectResult] = field(default_factory=list)


@dataclass
class TitleScanResult:
    """Typed outcome of one title scan; ``next_start`` is the live cursor."""

    title: str
    found: bool
    found_page: int | None
    scanned_pages: list[int]
    next_start: int | None
    rounds: list[ScanRound] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "found": self.found,
            "found_page": self.found_page,
            "scanned_pages": list(self.scanned_pages),
            "next_start": self.next_start,
            "rounds": [
                {
                    "pages": list(item.pages),
                    "found": item.found,
                    "found_page": item.found_page,
                    "error": item.error,
                    "page_results": [
                        {
                            "page": pr.page,
                            "found": pr.found,
                            "reason": pr.reason,
                            "error": pr.error,
                        }
                        for pr in item.page_results
                    ],
                }
                for item in self.rounds
            ],
        }


def _inspect_one_page(
    *,
    ctx: ToolContext,
    title: str,
    page: int,
    rendered_page: dict[str, Any],
) -> PageInspectResult:
    result = inspect_pages(
        ctx,
        {
            "pages": [page],
            "page_cap": 1,
            "question": build_section_start_page_question(title),
            "answer_keys": SECTION_START_PAGE_ANSWER_KEYS,
            "folder_name": "calibration_scan",
            "prefix": "scan",
            "usage_task": "calibration.scan_title_forward",
            "rendered_pages": [rendered_page],
        },
    )
    if result.status != "ok":
        return PageInspectResult(
            page=page, found=False, error=result.error or "inspect.pages failed"
        )
    fields = (result.payload or {}).get("fields") or {}
    return PageInspectResult(
        page=page,
        found=coerce_found(fields.get("found")),
        reason=coerce_reason(fields.get("reason")),
    )


def _inspect_pages_concurrent(
    *,
    ctx: ToolContext,
    title: str,
    pages: list[int],
) -> list[PageInspectResult]:
    """Serial window render, then concurrent single-page VLM inspect."""
    rendered = render_pages(
        ctx,
        pages,
        folder_name="calibration_scan",
        prefix="scan",
        timeout=120,
    )
    rendered_by_page = {
        int(item["page"]): {
            "page": int(item["page"]),
            "png_path": str(item["png_path"]),
        }
        for item in rendered
        if item.get("page") is not None and item.get("png_path")
    }
    missing = [page for page in pages if page not in rendered_by_page]
    if missing:
        return [
            PageInspectResult(
                page=page,
                found=False,
                error=(
                    f"render failed for pages={missing}"
                    if page in missing
                    else "render incomplete"
                ),
            )
            for page in pages
        ]

    if len(pages) == 1:
        page = pages[0]
        return [
            _inspect_one_page(
                ctx=ctx,
                title=title,
                page=page,
                rendered_page=rendered_by_page[page],
            )
        ]

    token_tracker_root_id = get_current_token_tracker_root_id()

    def _inspect_one_page_with_tracking(page: int) -> PageInspectResult:
        with bind_token_tracker(token_tracker_root_id):
            return _inspect_one_page(
                ctx=ctx,
                title=title,
                page=page,
                rendered_page=rendered_by_page[page],
            )

    by_page: dict[int, PageInspectResult] = {}
    with ThreadPoolExecutor(max_workers=len(pages)) as pool:
        futures = {
            pool.submit(_inspect_one_page_with_tracking, page): page
            for page in pages
        }
        for future in as_completed(futures):
            page = futures[future]
            try:
                by_page[page] = future.result()
            except Exception as exc:  # noqa: BLE001 — surface as page error
                by_page[page] = PageInspectResult(
                    page=page, found=False, error=str(exc)
                )
    return [by_page[page] for page in pages]


def scan_title_forward(
    *,
    ctx: ToolContext,
    title: str,
    start_page: int,
    page_count: int,
    window_schedule: tuple[int, ...] = DEFAULT_WINDOW_SCHEDULE,
) -> TitleScanResult:
    """Scan forward from ``start_page`` until the title is found or rounds run out.

    Each round covers ``window_schedule[i]`` consecutive pages starting at the
    cursor left by the previous round. The window is rendered once serially,
    then each page is inspected via VLM concurrently; the earliest true page
    wins. A page error without a hit is logged and the scan continues to the
    next window.
    """
    scanned: list[int] = []
    rounds: list[ScanRound] = []
    next_start = max(int(start_page), 1)

    for pages in progressive_page_windows(
        start_page=start_page,
        end_page=page_count,
        window_schedule=window_schedule,
    ):
        page_results = _inspect_pages_concurrent(ctx=ctx, title=title, pages=pages)
        next_start = pages[-1] + 1
        scanned.extend(pages)

        hits = [pr.page for pr in page_results if pr.found]
        if hits:
            found_page = min(hits)
            rounds.append(
                ScanRound(
                    pages=pages,
                    found=True,
                    found_page=found_page,
                    page_results=page_results,
                )
            )
            logger.info(
                "[calibration.scan] title={!r} found on page={} after {} round(s)",
                title,
                found_page,
                len(rounds),
            )
            return TitleScanResult(
                title=title,
                found=True,
                found_page=found_page,
                scanned_pages=scanned,
                next_start=next_start if next_start <= page_count else None,
                rounds=rounds,
            )

        errors = [pr.error for pr in page_results if pr.error]
        if errors:
            logger.warning(
                "[calibration.scan] title={!r} pages={} inspect failed: {}; "
                "continuing to next window",
                title,
                pages,
                errors[0],
            )
        rounds.append(
            ScanRound(
                pages=pages,
                found=False,
                found_page=None,
                error=errors[0] if errors else "",
                page_results=page_results,
            )
        )

    logger.info(
        "[calibration.scan] title={!r} not found in pages={}",
        title,
        scanned,
    )
    return TitleScanResult(
        title=title,
        found=False,
        found_page=None,
        scanned_pages=scanned,
        next_start=next_start if next_start <= page_count else None,
        rounds=rounds,
    )
