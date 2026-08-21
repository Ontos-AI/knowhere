"""PyMuPDF helpers used by document-agent tools."""

from __future__ import annotations

import gc
from dataclasses import dataclass
from typing import Any

from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)
from app.services.document_parser.structure.body_boundary import normalize_heading_label


@dataclass(frozen=True)
class PageTextBands:
    """Per-page text from one span pass: full content plus edge-band extracts.

    ``content`` is the full page text (includes header/footer span text).
    ``header`` / ``footer`` are the same spans whose vertical centers fall in the
    coarse ``header_y`` / ``footer_y`` bands. Strip tools remove those extracts
    from a search view; they do not rewrite the stored ``content``.
    """

    content: str
    header: str = ""
    footer: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "content": self.content,
            "header": self.header,
            "footer": self.footer,
        }

    @classmethod
    def from_any(cls, value: Any) -> "PageTextBands":
        if isinstance(value, PageTextBands):
            return value
        if isinstance(value, str):
            return cls(content=value)
        if isinstance(value, dict):
            content = value.get("content")
            if content is None and "text" in value:
                content = value.get("text")
            return cls(
                content=str(content or ""),
                header=str(value.get("header") or ""),
                footer=str(value.get("footer") or ""),
            )
        return cls(content=str(value or ""))


def page_content(value: Any) -> str:
    return PageTextBands.from_any(value).content


def page_content_map(raw: Any) -> dict[int, str]:
    """Map page -> content string (legacy-compatible plain cache shape)."""
    if not isinstance(raw, dict):
        return {}
    out: dict[int, str] = {}
    for page, value in raw.items():
        out[int(page)] = page_content(value)
    return out


def page_bands_map(raw: Any) -> dict[int, PageTextBands]:
    if not isinstance(raw, dict):
        return {}
    return {int(page): PageTextBands.from_any(value) for page, value in raw.items()}


def strip_margin_text(content: str, margin: str) -> str:
    """Remove one margin extract from full content (homologous span join).

    Tries the full margin blob first, then each non-empty line once, so
    non-contiguous edge lines still drop when they appear as content lines.
    """
    if not content or not margin:
        return content
    if margin in content:
        return content.replace(margin, "", 1)
    out = content
    for frag in margin.split("\n"):
        if frag and frag in out:
            out = out.replace(frag, "", 1)
    return out


def _band_for_center(
    cy: float,
    *,
    page_h: float,
    header_y: float | None,
    footer_y: float | None,
) -> str:
    if page_h <= 0:
        return "content"
    if header_y is not None and cy < float(header_y) * page_h:
        return "header"
    if footer_y is not None and cy > float(footer_y) * page_h:
        return "footer"
    return "content"


def _extract_page_bands_from_pymupdf_page(
    page: Any,
    *,
    header_y: float | None,
    footer_y: float | None,
) -> PageTextBands:
    """Build content/header/footer from the same span walk (line-join with ``\\n``)."""
    page_h = float(getattr(page.rect, "height", 0) or 0)
    data = page.get_text("dict") or {}
    content_lines: list[str] = []
    header_chunks: list[str] = []
    footer_chunks: list[str] = []

    for block in data.get("blocks") or []:
        if int(block.get("type") or 0) != 0:
            continue
        for line in block.get("lines") or []:
            line_content_parts: list[str] = []
            line_header_parts: list[str] = []
            line_footer_parts: list[str] = []
            for span in line.get("spans") or []:
                text = str(span.get("text") or "")
                if not text:
                    continue
                bbox = span.get("bbox") or (0, 0, 0, 0)
                try:
                    y0 = float(bbox[1])
                    y1 = float(bbox[3])
                except (TypeError, ValueError, IndexError):
                    y0, y1 = 0.0, 0.0
                cy = (y0 + y1) / 2.0
                band = _band_for_center(
                    cy, page_h=page_h, header_y=header_y, footer_y=footer_y
                )
                line_content_parts.append(text)
                if band == "header":
                    line_header_parts.append(text)
                elif band == "footer":
                    line_footer_parts.append(text)
            if line_content_parts:
                content_lines.append("".join(line_content_parts))
            if line_header_parts:
                header_chunks.append("".join(line_header_parts))
            if line_footer_parts:
                footer_chunks.append("".join(line_footer_parts))

    return PageTextBands(
        content="\n".join(content_lines),
        header="\n".join(header_chunks),
        footer="\n".join(footer_chunks),
    )


@worker
def _read_page_texts_worker(queue, pdf_path: str, pages: list[int]) -> None:
    import pymupdf  # type: ignore[import]

    texts: dict[int, str] = {}
    try:
        doc = pymupdf.open(pdf_path)
        for page in pages:
            idx = page - 1
            if 0 <= idx < doc.page_count:
                texts[page] = str(doc[idx].get_text() or "")
    finally:
        try:
            doc.close()
        except Exception:
            pass
        gc.collect()
    queue.put({"ok": True, "texts": texts})


@worker
def _read_page_text_bands_worker(
    queue,
    pdf_path: str,
    pages: list[int],
    header_y: float | None,
    footer_y: float | None,
) -> None:
    import pymupdf  # type: ignore[import]

    bands: dict[int, dict[str, str]] = {}
    try:
        doc = pymupdf.open(pdf_path)
        for page in pages:
            idx = page - 1
            if 0 <= idx < doc.page_count:
                record = _extract_page_bands_from_pymupdf_page(
                    doc[idx],
                    header_y=header_y,
                    footer_y=footer_y,
                )
                bands[page] = record.to_dict()
    finally:
        try:
            doc.close()
        except Exception:
            pass
        gc.collect()
    queue.put({"ok": True, "bands": bands})


def coerce_page_text_cache(raw: Any) -> dict[int, str]:
    """Legacy helper: page -> content string only."""
    return page_content_map(raw)


def read_page_texts(
    pdf_path: str,
    pages: list[int],
    *,
    timeout: int = 180,
) -> dict[int, str]:
    """Plain ``get_text()`` map for call sites that need a one-shot full dump.

    PROFILE text-scan uses :func:`read_page_text_bands` instead.
    """
    if not pages:
        return {}
    result = run_in_child_process(
        _read_page_texts_worker, pdf_path, pages, timeout=timeout
    )
    return {int(k): str(v) for k, v in (result.get("texts") or {}).items()}


def read_page_text_bands(
    pdf_path: str,
    pages: list[int],
    *,
    header_y: float | None = None,
    footer_y: float | None = None,
    timeout: int = 180,
) -> dict[int, PageTextBands]:
    """Span-homogeneous content/header/footer for PROFILE text scan."""
    if not pages:
        return {}
    result = run_in_child_process(
        _read_page_text_bands_worker,
        pdf_path,
        pages,
        header_y,
        footer_y,
        timeout=timeout,
    )
    raw_bands = result.get("bands") or {}
    return {
        int(page): PageTextBands.from_any(value)
        for page, value in raw_bands.items()
    }


def meaningful_lines(text: str) -> list[str]:
    lines = [normalize_heading_label(line) for line in text.splitlines()]
    return [line for line in lines if line]


def top_lines(text: str, *, max_lines: int = 20) -> list[str]:
    lines = meaningful_lines(text)
    return lines[: max(max_lines, 0)]


def compact_payload_keys(payload: dict[str, Any]) -> list[str]:
    return sorted(str(key) for key in payload.keys())
