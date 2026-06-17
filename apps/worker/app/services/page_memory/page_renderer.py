"""Page renderer: produce PNG, thumbnail, and raw text for each page.

Wraps the existing ``document_agent/visual.render_pages`` and
``pdf_text.read_page_texts`` utilities, adding thumbnail generation
(72 dpi), dimensions, and landscape detection.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from app.services.document_agent.manifest import PageFeature, ToolContext
from app.services.document_agent.pdf_text import read_page_texts
from app.services.document_agent.visual import render_pages


@dataclass(frozen=True)
class PageRenderResult:
    """Output of rendering a single page."""

    page_index: int
    """1-based page number."""

    image_path: str
    """Absolute path to full-resolution PNG (``pages/page-N.png``)."""

    thumb_path: str
    """Absolute path to thumbnail JPEG (``pages/thumb-N.jpg``)."""

    raw_text: str
    """PyMuPDF extracted text for this page."""

    width: float
    """Page width in points."""

    height: float
    """Page height in points."""

    is_landscape: bool
    """``True`` when *width > height*."""


def render_document_pages(
    *,
    pdf_path: str,
    page_count: int,
    output_dir: str,
    page_features: list[PageFeature] | None = None,
    ctx: ToolContext | None = None,
    dpi: int = 144,
    thumb_dpi: int = 72,
    timeout: int = 300,
) -> list[PageRenderResult]:
    """Render every page in *pdf_path* as PNG + thumb + raw text.

    Parameters
    ----------
    pdf_path:
        Local filesystem path to the PDF.
    page_count:
        Total page count (avoids re-opening the PDF).
    output_dir:
        Root output directory; pages are written to ``output_dir/pages/``.
    page_features:
        If available, dimensions are read from here (avoiding a second
        PyMuPDF open).  Otherwise falls back to 0/0/False.
    ctx:
        An optional ``ToolContext`` forwarded to ``render_pages``.  If
        *None*, a lightweight context is constructed internally.
    dpi:
        Full-resolution DPI (default 144).
    thumb_dpi:
        Thumbnail DPI (default 72).
    timeout:
        PyMuPDF subprocess timeout in seconds.

    Returns
    -------
    list[PageRenderResult]
        One entry per page, ordered by page_index.
    """
    pages = list(range(1, page_count + 1))
    if not pages:
        return []

    # ── raw text ──────────────────────────────────────────────────────
    page_texts = read_page_texts(pdf_path, pages, timeout=timeout)

    # ── full-resolution PNGs ──────────────────────────────────────────
    if ctx is not None:
        pngs = render_pages(
            ctx,
            pages,
            folder_name="pages",
            prefix="page",
            dpi=dpi,
            timeout=timeout,
        )
    else:
        from app.services.document_agent.state import AgentBlackboard

        blackboard = AgentBlackboard()
        blackboard.page_count = page_count
        tmp_ctx = ToolContext(
            pdf_path=pdf_path,
            job_id="page_renderer",
            blackboard=blackboard,
            budget=None,
            trace=None,
            output_dir=output_dir,
            settings={"agent_png_dpi": str(dpi)},
        )
        pngs = render_pages(
            tmp_ctx,
            pages,
            folder_name="pages",
            prefix="page",
            dpi=dpi,
            timeout=timeout,
        )

    png_map: dict[int, str] = {
        item["page"]: item["png_path"] for item in pngs if item.get("png_path")
    }

    # ── thumbnails (downsample PNG → JPEG at thumb_dpi) ───────────────
    thumb_map = _generate_thumbnails(
        png_map, output_dir=output_dir, dpi=dpi, thumb_dpi=thumb_dpi,
    )

    # ── page dimensions from page_features ────────────────────────────
    feature_map: dict[int, PageFeature] = {}
    if page_features:
        for feat in page_features:
            feature_map[feat.page] = feat

    # ── assemble results ──────────────────────────────────────────────
    results: list[PageRenderResult] = []
    for page in pages:
        feat = feature_map.get(page)
        results.append(
            PageRenderResult(
                page_index=page,
                image_path=png_map.get(page, ""),
                thumb_path=thumb_map.get(page, ""),
                raw_text=page_texts.get(page, ""),
                width=feat.width if feat else 0.0,
                height=feat.height if feat else 0.0,
                is_landscape=(
                    feat.orientation == "landscape" if feat else False
                ),
            )
        )

    logger.info(
        "[page_renderer] rendered {} pages ({} PNGs, {} thumbs)",
        len(results),
        len(png_map),
        len(thumb_map),
    )
    return results


def _generate_thumbnails(
    png_map: dict[int, str],
    *,
    output_dir: str,
    dpi: int,
    thumb_dpi: int,
) -> dict[int, str]:
    """Downsample full-resolution PNGs to JPEG thumbnails."""
    thumb_dir = os.path.join(output_dir, "pages")
    os.makedirs(thumb_dir, exist_ok=True)

    try:
        from PIL import Image  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "[page_renderer] Pillow not available; skipping thumbnail generation"
        )
        return {}

    scale = thumb_dpi / max(dpi, 1)
    thumb_map: dict[int, str] = {}

    for page, png_path in png_map.items():
        if not os.path.exists(png_path):
            continue
        try:
            with Image.open(png_path) as img:
                new_size = (
                    max(int(img.width * scale), 1),
                    max(int(img.height * scale), 1),
                )
                thumb = img.resize(new_size, Image.LANCZOS)
                thumb_name = f"thumb_{page}.jpg"
                thumb_path = os.path.join(thumb_dir, thumb_name)
                thumb.convert("RGB").save(thumb_path, "JPEG", quality=75)
                thumb_map[page] = thumb_path
        except Exception as exc:
            logger.warning(
                "[page_renderer] thumbnail failed for page {}: {}", page, exc,
            )

    return thumb_map
