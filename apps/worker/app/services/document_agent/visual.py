"""Shared page rendering helpers for document-agent visual reasoning."""

from __future__ import annotations

import gc
import os
from pathlib import Path
from typing import Any

from app.services.document_agent.manifest import ToolContext
from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)


@worker
def _render_pages_worker(
    queue,
    pdf_path: str,
    pages: list[int],
    output_dir: str,
    dpi: int,
    prefix: str,
) -> None:
    import pymupdf  # type: ignore[import]

    results: list[dict[str, Any]] = []
    try:
        doc = pymupdf.open(pdf_path)
        for page_num in pages:
            idx = page_num - 1
            if 0 <= idx < doc.page_count:
                page = doc[idx]
                mat = pymupdf.Matrix(dpi / 72.0, dpi / 72.0)
                pix = page.get_pixmap(matrix=mat)
                png_name = f"{prefix}_page_{page_num}.png"
                png_path = os.path.join(output_dir, png_name)
                pix.save(png_path)
                results.append({"page": page_num, "png_path": png_path})
    finally:
        try:
            doc.close()
        except Exception:
            pass
        gc.collect()
    queue.put({"ok": True, "results": results})


def visual_output_dir(ctx: ToolContext, folder_name: str = "agent_visuals") -> str:
    output_dir = str(
        Path(ctx.output_dir or os.path.expanduser("~/.knowhere/_debug_profile"))
        / folder_name
    )
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


def render_pages(
    ctx: ToolContext,
    pages: list[int],
    *,
    folder_name: str = "agent_visuals",
    prefix: str = "visual",
    dpi: int | None = None,
    timeout: int = 120,
) -> list[dict[str, Any]]:
    if not pages:
        return []
    page_count = max(int(ctx.blackboard.page_count or 0), 0)
    bounded_pages = sorted({page for page in pages if 1 <= page <= page_count})
    if not bounded_pages:
        return []
    output_dir = visual_output_dir(ctx, folder_name=folder_name)
    effective_dpi = dpi or int(ctx.settings.get("agent_png_dpi", "144"))
    result = run_in_child_process(
        _render_pages_worker,
        ctx.pdf_path,
        bounded_pages,
        output_dir,
        effective_dpi,
        prefix,
        timeout=timeout,
    )
    return list(result.get("results") or [])

