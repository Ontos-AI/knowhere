"""Screen every page image for structural pages before TOC profiling.

Every page goes into numbered grids; one VLM call per grid names the pages
that list sections (TOC / agenda) and the pages that open a section
(dividers). The result is written to ``blackboard.page_screen`` for the
structure routes to use.
"""

from __future__ import annotations

import time

from loguru import logger

from app.services.document_agent.manifest import PageScreen, ToolContext, ToolResult
from app.services.document_agent.page_screen.config import PageScreenConfig
from app.services.document_agent.page_screen.contact_sheet import ContactSheets
from app.services.document_agent.page_screen.prompts import SCREEN_PROMPT
from app.services.document_agent.page_screen.vision import (
    PageGrids,
    VlmCall,
    page_numbers,
    parallel_vlm,
    vision_call,
)

PAGE_SCREEN_VISUAL_DIR = "page_screen"
_LOG_PREFIX = "[profile.page_screen]"


def run_page_screen(ctx: ToolContext) -> ToolResult:
    """Screen the pages and write ``blackboard.page_screen``.

    A failed screen leaves ``page_screen`` unset and degrades like a failed
    TOC extraction: the profile goes on and the routes see no screen.
    """
    started = time.monotonic()
    config = PageScreenConfig.from_settings()
    usage = {"calls": 0, "tokens": 0}
    try:
        screen = screen_pages(
            page_count=int(ctx.blackboard.page_count or 0),
            grids=ContactSheets(ctx, folder_name=PAGE_SCREEN_VISUAL_DIR),
            config=config,
            vlm=vision_call(
                ctx,
                max_tokens=config.vlm_max_tokens,
                usage=usage,
                usage_scope="page_screen",
            ),
        )
    except Exception as exc:
        logger.warning("{} screen failed: {}", _LOG_PREFIX, exc)
        return ToolResult(
            status="ok",
            latency_ms=int((time.monotonic() - started) * 1000),
            tokens_used=usage["tokens"],
            output_summary={"vlm_calls": usage["calls"]},
            warnings=[f"Page screen failed: {exc}"],
        )
    ctx.blackboard.page_screen = screen
    logger.info(
        "{} toc_pages={} divider_pages={} calls={} tokens={}",
        _LOG_PREFIX,
        screen.toc_pages,
        screen.divider_pages,
        usage["calls"],
        usage["tokens"],
    )
    return ToolResult(
        status="ok",
        payload=screen.to_dict(),
        latency_ms=int((time.monotonic() - started) * 1000),
        tokens_used=usage["tokens"],
        output_summary={
            "toc_pages": len(screen.toc_pages),
            "divider_pages": len(screen.divider_pages),
            "vlm_calls": usage["calls"],
        },
    )


def screen_pages(
    *, page_count: int, grids: PageGrids, config: PageScreenConfig, vlm: VlmCall
) -> PageScreen:
    pages = list(range(1, page_count + 1))
    chunks = [
        pages[start : start + config.sheet.cells]
        for start in range(0, len(pages), config.sheet.cells)
    ]
    images = grids.grids(pages, config.sheet, name="screen")
    answers = parallel_vlm(
        [(SCREEN_PROMPT, [image], "screen") for image in images],
        vlm=vlm,
        concurrency=config.vlm_concurrency,
    )
    toc_pages: set[int] = set()
    divider_pages: set[int] = set()
    for chunk, answer in zip(chunks, answers):
        # A page counts only on the sheet that shows it.
        shown = set(chunk)
        toc_pages.update(page_numbers(answer.get("toc_pages"), shown))
        divider_pages.update(page_numbers(answer.get("divider_pages"), shown))
    return PageScreen(toc_pages=sorted(toc_pages), divider_pages=sorted(divider_pages))
