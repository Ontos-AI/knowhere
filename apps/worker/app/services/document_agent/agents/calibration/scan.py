"""Deterministic forward scan for a TOC title via ``inspect.pages``.

A TOC entry gives a printed page label; the physical page is that candidate or
some page after it. The scan walks forward from the candidate with a widening
window, feeding each round's cursor into the next one, so a miss never re-opens
pages that were already inspected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from app.services.document_agent.agents.calibration.prompts import (
    SECTION_START_ANSWER_KEYS,
    build_section_start_question,
    coerce_found,
    coerce_found_page,
)
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.tools.inspect_pages import inspect_pages

DEFAULT_WINDOW_SCHEDULE: tuple[int, ...] = (2, 4, 6, 10)


@dataclass
class ScanRound:
    pages: list[int]
    found: bool
    found_page: int | None = None
    error: str = ""


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
                }
                for item in self.rounds
            ],
        }


def scan_title_forward(
    *,
    ctx: ToolContext,
    title: str,
    start_page: int,
    page_count: int,
    window_schedule: tuple[int, ...] = DEFAULT_WINDOW_SCHEDULE,
) -> TitleScanResult:
    """Scan forward from ``start_page`` until the title is found or rounds run out.

    Each round opens ``window_schedule[i]`` consecutive pages starting at the
    cursor left by the previous round, so no page is inspected twice.
    """
    cursor = max(int(start_page), 1)
    scanned: list[int] = []
    rounds: list[ScanRound] = []

    for size in window_schedule:
        if cursor > page_count:
            break
        pages = [page for page in range(cursor, cursor + size) if page <= page_count]
        if not pages:
            break

        result = inspect_pages(
            ctx,
            {
                "pages": pages,
                "page_cap": len(pages),
                "question": build_section_start_question(title),
                "answer_keys": SECTION_START_ANSWER_KEYS,
                "folder_name": "calibration_scan",
                "prefix": "scan",
                "usage_task": "calibration.scan_title_forward",
                "visual_stage": "calibration",
            },
        )
        cursor = pages[-1] + 1
        scanned.extend(pages)

        if result.status != "ok":
            rounds.append(ScanRound(pages=pages, found=False, error=result.error or ""))
            logger.warning(
                "[calibration.scan] title={!r} pages={} inspect failed: {}",
                title,
                pages,
                result.error,
            )
            break

        fields = (result.payload or {}).get("fields") or {}
        found_page = coerce_found_page(fields.get("found_page"), pages=pages)
        found = coerce_found(fields.get("found")) and found_page is not None
        rounds.append(ScanRound(pages=pages, found=found, found_page=found_page))
        if found:
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
                next_start=cursor if cursor <= page_count else None,
                rounds=rounds,
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
        next_start=cursor if cursor <= page_count else None,
        rounds=rounds,
    )
