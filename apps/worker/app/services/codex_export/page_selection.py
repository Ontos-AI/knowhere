"""Deterministic page selection and lossless review-page rendering."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import pymupdf

from app.services.codex_export.schema import DocumentBlock


MIN_RENDER_DPI = 72
MAX_RENDER_DPI = 300


def render_document_pages(**kwargs: Any) -> Any:
    """Lazily load the platform renderer so standalone imports need no app config."""
    os.environ.setdefault("PYMUPDF_MAX_CONCURRENT", "1")
    from app.services.page_memory.page_renderer import (
        render_document_pages as platform_render_document_pages,
    )
    from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
        shutdown_pymupdf_process_pool,
    )

    try:
        return platform_render_document_pages(**kwargs)
    finally:
        shutdown_pymupdf_process_pool()


@dataclass(frozen=True)
class RenderedPage:
    page_number: int
    output_path: Path
    dpi: int
    width_points: float
    height_points: float
    page_number_semantics: str = "native_pdf"


def _validate_pages(pages: Sequence[int], page_count: int) -> list[int]:
    if page_count < 0:
        raise ValueError("PDF page count must not be negative.")
    selected = sorted(set(pages))
    invalid = [page for page in selected if page < 1 or page > page_count]
    if invalid:
        raise ValueError(
            f"Selected page is outside the rendered PDF range 1-{page_count}: {invalid}"
        )
    return selected


def resolve_selected_pages(
    *,
    requested_pages: Sequence[int],
    blocks: Sequence[DocumentBlock],
    include_table_pages: bool,
    include_image_pages: bool,
    page_count: int,
) -> list[int]:
    """Resolve explicit and structural page selections without semantic inference."""
    selected = set(requested_pages)
    is_office = any(
        block.source_locator.get("kind") == "office_logical_page" for block in blocks
    )
    if not is_office:
        for block in blocks:
            include = (include_table_pages and block.block_type == "table") or (
                include_image_pages and block.block_type in {"image", "chart"}
            )
            if include:
                page = block.source_locator.get("page_number")
                if isinstance(page, int):
                    selected.add(page)
    return _validate_pages(tuple(selected), page_count)


def _pdf_dimensions(pdf_path: Path) -> tuple[int, dict[int, tuple[float, float]]]:
    try:
        document = pymupdf.open(pdf_path)
    except Exception as error:
        raise ValueError(f"Unable to open PDF for page rendering: {pdf_path.name}") from error
    try:
        dimensions: dict[int, tuple[float, float]] = {}
        for index in range(document.page_count):
            page = document.load_page(index)
            dimensions[index + 1] = (
                float(page.rect.width),
                float(page.rect.height),
            )
        return document.page_count, dimensions
    finally:
        document.close()


def render_review_pages(
    *,
    pdf_path: Path,
    pages: Sequence[int],
    output_dir: Path,
    dpi: int,
    page_number_semantics: str = "native_pdf",
) -> list[RenderedPage]:
    """Render selected one-based PDF pages to zero-padded lossless PNG files."""
    if not MIN_RENDER_DPI <= dpi <= MAX_RENDER_DPI:
        raise ValueError(
            f"DPI must be between {MIN_RENDER_DPI} and {MAX_RENDER_DPI}."
        )
    source_pdf = pdf_path.expanduser().resolve()
    if not source_pdf.is_file():
        raise ValueError("PDF path must be an existing local file.")
    page_count, dimensions = _pdf_dimensions(source_pdf)
    selected = _validate_pages(pages, page_count)
    if not selected:
        return []

    destination_dir = output_dir.expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    rendered = render_document_pages(
        pdf_path=str(source_pdf),
        page_count=page_count,
        output_dir=str(destination_dir.parent),
        pages=selected,
        page_features=None,
        page_texts={},
        dpi=dpi,
    )
    by_page = {item.page_index: item for item in rendered}
    results: list[RenderedPage] = []
    for page_number in selected:
        item = by_page.get(page_number)
        if item is None or not item.image_path:
            raise RuntimeError(f"Page renderer did not produce page {page_number}.")
        rendered_path = Path(item.image_path).resolve()
        if not rendered_path.is_file():
            raise RuntimeError(f"Page renderer output is missing for page {page_number}.")
        destination = destination_dir / f"page-{page_number:04d}.png"
        if rendered_path != destination:
            if destination.exists():
                destination.unlink()
            shutil.move(str(rendered_path), destination)
        width, height = dimensions[page_number]
        results.append(
            RenderedPage(
                page_number=page_number,
                output_path=destination,
                dpi=dpi,
                width_points=width,
                height_points=height,
                page_number_semantics=page_number_semantics,
            )
        )
    return results
