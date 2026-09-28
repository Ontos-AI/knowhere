"""PyMuPDF helpers used by document-agent tools."""

from __future__ import annotations

import gc
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)
from app.services.document_parser.structure.body_boundary import (
    normalize_heading_label,
    normalize_match_text,
)

LineRegion = Literal["header", "footer", "body"]

# Characters str.splitlines() treats as line boundaries; they must never appear
# inside one line, or content.splitlines() indices drift from line_index.
_SPLITLINES_BREAKS_RE = re.compile("[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")

# Height / font size are stored at this precision so float jitter from the same
# PDF style yields equal values, and fingerprints can compare them directly.
_GEOMETRY_DECIMALS = 2


def sanitize_line_text(text: str) -> str:
    return _SPLITLINES_BREAKS_RE.sub(" ", text or "")


@dataclass(frozen=True)
class PageTextLine:
    text: str
    region: LineRegion
    line_index: int
    height: float | None = None
    font: str | None = None
    font_size: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PageTextLine":
        return cls(
            text=str(data["text"]),
            region=data["region"],
            line_index=int(data["line_index"]),
            height=None if data.get("height") is None else float(data["height"]),
            font=data.get("font") or None,
            font_size=None if data.get("font_size") is None else float(data["font_size"]),
        )


@dataclass(frozen=True)
class LineDraft:
    """Extractor-side line before sanitizing and index assignment."""

    text: str
    region: LineRegion = "body"
    height: float | None = None
    font: str | None = None
    font_size: float | None = None


@dataclass(frozen=True)
class PageTextBands:
    """Per-page ordered line records; content/header/footer are derived views."""

    lines: tuple[PageTextLine, ...] = ()

    def __post_init__(self) -> None:
        for position, line in enumerate(self.lines):
            if line.line_index != position:
                raise ValueError(
                    f"line_index {line.line_index} does not match position {position}"
                )
            if not line.text.strip() or _SPLITLINES_BREAKS_RE.search(line.text):
                raise ValueError(f"invalid line text at line_index {position}")

    @property
    def content(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def header(self) -> str:
        return "\n".join(line.text for line in self.lines if line.region == "header")

    @property
    def footer(self) -> str:
        return "\n".join(line.text for line in self.lines if line.region == "footer")

    def to_dict(self) -> dict[str, Any]:
        return {"lines": [line.to_dict() for line in self.lines]}

    @classmethod
    def from_text(cls, content: str) -> "PageTextBands":
        return build_page_text_bands(
            LineDraft(text=raw) for raw in content.splitlines()
        )

    @classmethod
    def from_any(cls, value: Any) -> "PageTextBands":
        if isinstance(value, PageTextBands):
            return value
        if isinstance(value, str):
            return cls.from_text(value)
        if isinstance(value, dict) and "lines" in value:
            return cls(
                lines=tuple(PageTextLine.from_dict(item) for item in value["lines"])
            )
        if isinstance(value, dict):
            raise ValueError(
                "legacy page_full_text_cache entry without 'lines'; re-run Stage 0"
            )
        return cls.from_text(str(value or ""))


def build_page_text_bands(drafts: Iterable[LineDraft]) -> PageTextBands:
    """Sanitize, drop blank lines, assign line_index in document order."""
    lines: list[PageTextLine] = []
    for draft in drafts:
        text = sanitize_line_text(draft.text)
        if not text.strip():
            continue
        lines.append(
            PageTextLine(
                text=text,
                region=draft.region,
                line_index=len(lines),
                height=draft.height,
                font=draft.font,
                font_size=draft.font_size,
            )
        )
    return PageTextBands(lines=tuple(lines))


def line_fingerprint(line: PageTextLine) -> str:
    """Text + render signature of one line; excludes line_index on purpose."""
    return "|".join(
        (
            normalize_match_text(line.text),
            line.region,
            "" if line.height is None else str(line.height),
            line.font or "",
            "" if line.font_size is None else str(line.font_size),
        )
    )


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


def strip_margin_text(
    content: str,
    margin: str,
    *,
    edge: Literal["header", "footer"],
) -> str:
    """Remove a header/footer band extract from page content for search view.

    Header is removed only from the content prefix; footer only from the
    content suffix. Never uses a whole-string first-occurrence replace (that
    can delete a same-looking body span). Falls back to edge-aligned line
    fragments when the full margin blob is not contiguous at that edge.
    """
    if not content or not margin:
        return content

    if edge == "header":
        if content.startswith(margin):
            return content[len(margin) :]
        out = content
        for frag in margin.split("\n"):
            if not frag:
                continue
            if out.startswith(frag + "\n"):
                out = out[len(frag) + 1 :]
            elif out.startswith(frag):
                out = out[len(frag) :]
            else:
                break
        return out

    if content.endswith(margin):
        return content[: -len(margin)]
    out = content
    for frag in reversed([part for part in margin.split("\n") if part]):
        if out.endswith("\n" + frag):
            out = out[: -(len(frag) + 1)]
        elif out.endswith(frag):
            out = out[: -len(frag)]
        else:
            break
    return out


def _band_for_center(
    cy: float,
    *,
    page_h: float,
    header_y: float | None,
    footer_y: float | None,
) -> LineRegion:
    if page_h <= 0:
        return "body"
    if header_y is not None and cy < float(header_y) * page_h:
        return "header"
    if footer_y is not None and cy > float(footer_y) * page_h:
        return "footer"
    return "body"


def _span_text(span: dict[str, Any]) -> str:
    return str(span.get("text") or "")


def _span_y(span: dict[str, Any]) -> tuple[float, float] | None:
    bbox = span.get("bbox")
    if not bbox:
        return None
    try:
        return float(bbox[1]), float(bbox[3])
    except (TypeError, ValueError, IndexError):
        return None


def _extract_page_bands_from_pymupdf_page(
    page: Any,
    *,
    header_y: float | None,
    footer_y: float | None,
) -> PageTextBands:
    """Build ordered line records from one span walk."""
    page_h = float(getattr(page.rect, "height", 0) or 0)
    data = page.get_text("dict") or {}
    drafts: list[LineDraft] = []

    for block in data.get("blocks") or []:
        if int(block.get("type") or 0) != 0:
            continue
        block_lines: list[tuple[str, float, float, dict[str, Any]]] = []
        for line in block.get("lines") or []:
            spans = [span for span in line.get("spans") or [] if _span_text(span)]
            text = sanitize_line_text("".join(_span_text(span) for span in spans))
            if not text.strip():
                continue
            ys = [y for y in (_span_y(span) for span in spans) if y is not None]
            y0 = min((y[0] for y in ys), default=0.0)
            y1 = max((y[1] for y in ys), default=0.0)
            main_span = max(spans, key=lambda span: len(_span_text(span)))
            block_lines.append((text, y0, y1, main_span))
        if not block_lines:
            continue
        block_height = round(
            max(item[2] for item in block_lines) - min(item[1] for item in block_lines),
            _GEOMETRY_DECIMALS,
        )
        for text, y0, y1, main_span in block_lines:
            size = main_span.get("size")
            drafts.append(
                LineDraft(
                    text=text,
                    region=_band_for_center(
                        (y0 + y1) / 2.0,
                        page_h=page_h,
                        header_y=header_y,
                        footer_y=footer_y,
                    ),
                    height=block_height,
                    font=str(main_span.get("font") or "") or None,
                    font_size=None
                    if size is None
                    else round(float(size), _GEOMETRY_DECIMALS),
                )
            )
    return build_page_text_bands(drafts)


@worker
def _read_page_texts_worker(queue, pdf_path: str, pages: list[int]) -> None:
    import pymupdf  # type: ignore[import]

    texts: dict[int, str] = {}
    doc = None
    try:
        doc = pymupdf.open(pdf_path)
        for page in pages:
            idx = page - 1
            if 0 <= idx < doc.page_count:
                texts[page] = str(doc[idx].get_text() or "")
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception as exc:
                print(f"Failed to close PDF document in _read_page_texts_worker: {exc}")
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

    bands: dict[int, Any] = {}
    doc = None
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
        if doc is not None:
            try:
                doc.close()
            except Exception as exc:
                print(f"Failed to close PDF document in _read_page_text_bands_worker: {exc}")
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
    """Ordered line records for PROFILE text scan."""
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
