"""Fine-stage verbatim page-text extraction (advanced, default off).

Called unconditionally after fine render. ``extractor="none"`` returns immediately
with no extra model or parser calls. ``vlm`` / ``ocr`` / ``mineru`` fill page text
used to overwrite M0 band content when the advanced switch is on. When ``vlm``
raises on a page, that page falls back to ``ocr``; when ``mineru`` raises, the
scope falls back to ``ocr``; ``ocr`` errors fall back to ``none`` (keeping M0).
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
import shutil
from typing import Any

from loguru import logger

from app.services.document_agent.tools.ocr_pages import ocr_rendered_pages
from shared.services.ai.summary.engine import transcribe


def _build_image_path_by_page(
    rendered: list[Any],
    pages: list[int],
) -> dict[int, str]:
    page_set = set(pages)
    image_path_by_page: dict[int, str] = {}
    for item in rendered:
        raw_page = getattr(item, "page_index", None)
        if raw_page is None:
            continue
        page = int(raw_page)
        if page not in page_set:
            continue
        image_path = str(getattr(item, "image_path", "") or "").strip()
        if image_path:
            image_path_by_page[page] = image_path
    return image_path_by_page


def _extract_via_ocr(
    *,
    image_path_by_page: Mapping[int, str],
    pages: list[int],
) -> dict[int, str]:
    subset = {
        page: image_path_by_page[page]
        for page in pages
        if page in image_path_by_page
    }
    if not subset:
        return {}
    try:
        return ocr_rendered_pages(subset)
    except Exception as exc:
        logger.warning(
            "[page_text_extractor] ocr failed; keeping M0 text: {}",
            exc,
        )
        return {}


def extract_fine_page_text(
    *,
    pdf_path: str,
    rendered: list[Any],
    final_pages: list[int],
    extractor: str,
    vlm_model: str | None,
    output_dir: str,
    concurrency: int,
) -> dict[int, str]:
    """Return ``{page: verbatim text}`` for the requested extractor.

    ``none`` (default) returns ``{}`` so callers keep M0 band text. Unknown
    extractor names also return ``{}`` so the default-off path cannot spend.
    """
    resolved = (extractor or "none").strip().lower()
    pages = [int(page) for page in final_pages if int(page) > 0]
    if resolved == "none" or not pages:
        return {}
    image_path_by_page = _build_image_path_by_page(rendered, pages)
    if resolved == "ocr":
        return _extract_via_ocr(
            image_path_by_page=image_path_by_page,
            pages=pages,
        )
    if resolved == "mineru":
        try:
            return _extract_via_mineru(
                pdf_path=pdf_path,
                final_pages=pages,
                output_dir=output_dir,
            )
        except Exception as exc:
            logger.warning(
                "[page_text_extractor] mineru failed ({}); falling back to ocr",
                exc,
            )
            return _extract_via_ocr(
                image_path_by_page=image_path_by_page,
                pages=pages,
            )
    if resolved == "vlm":
        return _extract_via_vlm(
            image_path_by_page=image_path_by_page,
            final_pages=pages,
            vlm_model=vlm_model,
            concurrency=concurrency,
        )
    return {}


def _extract_via_vlm(
    *,
    image_path_by_page: Mapping[int, str],
    final_pages: list[int],
    vlm_model: str | None,
    concurrency: int,
) -> dict[int, str]:
    jobs = [
        (page, str(image_path_by_page.get(page) or ""))
        for page in final_pages
    ]
    if not jobs:
        return {}

    import gevent
    from gevent.pool import Pool as GeventPool

    def _one(page: int, image_path: str) -> tuple[int, str, Exception | None]:
        try:
            text = transcribe(
                image_paths=[image_path],
                model=vlm_model,
                usage_task="page_memory.fine_transcribe",
            )
            return page, (text or "").strip(), None
        except Exception as exc:
            return page, "", exc

    pool = GeventPool(size=min(max(1, concurrency), len(jobs)))
    greenlets = [pool.spawn(_one, page, image_path) for page, image_path in jobs]
    gevent.joinall(greenlets, raise_error=False)
    extracted: dict[int, str] = {}
    failed_pages: list[int] = []
    for (job_page, _), greenlet in zip(jobs, greenlets, strict=True):
        if greenlet.value is None:
            exc = getattr(greenlet, "exception", None)
            logger.warning(
                "[page_text_extractor] vlm transcribe failed on page {} ({}); falling back to ocr",
                job_page,
                exc,
            )
            failed_pages.append(int(job_page))
            continue
        page, text, error = greenlet.value
        if error is not None:
            logger.warning(
                "[page_text_extractor] vlm transcribe failed on page {} ({}); falling back to ocr",
                page,
                error,
            )
            failed_pages.append(int(page))
            continue
        if text:
            extracted[int(page)] = text
    if failed_pages:
        extracted.update(
            _extract_via_ocr(
                image_path_by_page=image_path_by_page,
                pages=failed_pages,
            )
        )
    return extracted


def _extract_via_mineru(
    *,
    pdf_path: str,
    final_pages: list[int],
    output_dir: str,
) -> dict[int, str]:
    from app.services.document_parser.formats.pdf.shard_splitter import (
        MergedShard,
        split_pdf,
    )
    from app.services.document_parser.providers.mineru.pdf_service import parse_via_full

    ordered = sorted({int(page) for page in final_pages})
    if not ordered:
        return {}
    work_dir = os.path.join(output_dir, "mineru")
    os.makedirs(work_dir, exist_ok=True)
    try:
        page_start = ordered[0]
        page_end = ordered[-1]
        exclude_pages = set(range(page_start, page_end + 1)) - set(ordered)
        live_shards = split_pdf(
            pdf_path,
            [MergedShard(0, page_start=page_start, page_end=page_end)],
            work_dir,
            exclude_pages=exclude_pages or None,
        )
        if not live_shards:
            return {}
        live = live_shards[0]
        parse_via_full(
            live.pdf_path,
            os.path.basename(live.pdf_path) or "scope.pdf",
            work_dir,
        )
        return _page_text_from_layout(
            os.path.join(work_dir, "layout.json"),
            live.page_numbers,
        )
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


def _page_text_from_layout(layout_json_path: str, page_numbers: list[int]) -> dict[int, str]:
    if not os.path.exists(layout_json_path):
        raise FileNotFoundError(f"MinerU layout.json missing at {layout_json_path}")
    with open(layout_json_path, encoding="utf-8") as handle:
        layout = json.load(handle)
    if not isinstance(layout, dict):
        raise ValueError("MinerU layout.json root is not a JSON object")
    extracted: dict[int, str] = {}
    for page in layout.get("pdf_info") or []:
        if not isinstance(page, dict):
            continue
        try:
            page_idx = int(page.get("page_idx", 0))
        except (TypeError, ValueError):
            continue
        if not (0 <= page_idx < len(page_numbers)):
            continue
        texts = _block_texts(page.get("para_blocks") or [])
        joined = "\n".join(part for part in texts if part)
        if joined:
            extracted[int(page_numbers[page_idx])] = joined
    return extracted


def _block_texts(blocks: list[Any]) -> list[str]:
    texts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        texts.extend(_block_texts(list(block.get("blocks") or [])))
        for line in block.get("lines") or []:
            if not isinstance(line, dict):
                continue
            line_parts: list[str] = []
            for span in line.get("spans") or []:
                if not isinstance(span, dict):
                    continue
                content = str(span.get("content") or "").strip()
                if content:
                    line_parts.append(content)
                    continue
                table_text = _table_html_text(span.get("html"))
                if table_text:
                    line_parts.append(table_text)
            if line_parts:
                texts.append(" ".join(line_parts))
    return texts


def _table_html_text(raw_html: Any) -> str:
    html = str(raw_html or "").strip()
    if not html:
        return ""
    from bs4 import BeautifulSoup, Tag

    soup = BeautifulSoup(html, "html.parser")
    rows: list[str] = []
    for tr in soup.find_all("tr"):
        if not isinstance(tr, Tag):
            continue
        cells = [
            " ".join(cell.get_text(" ", strip=True).split())
            for cell in tr.find_all(["th", "td"])
        ]
        row_text = " | ".join(cell for cell in cells if cell)
        if row_text:
            rows.append(row_text)
    if rows:
        return "\n".join(rows)
    return " ".join(soup.get_text(" ", strip=True).split())
