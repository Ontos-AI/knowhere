"""PyMuPDF helpers used by document-agent tools."""

from __future__ import annotations

import gc
from typing import Any

from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)


def normalize_spaces(text: str) -> str:
    return " ".join((text or "").split())


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


def read_page_texts(
    pdf_path: str,
    pages: list[int],
    *,
    timeout: int = 180,
) -> dict[int, str]:
    if not pages:
        return {}
    result = run_in_child_process(_read_page_texts_worker, pdf_path, pages, timeout=timeout)
    return {int(k): str(v) for k, v in (result.get("texts") or {}).items()}


def meaningful_lines(text: str) -> list[str]:
    return [normalize_spaces(line) for line in text.splitlines() if normalize_spaces(line)]


def top_lines(text: str, *, max_lines: int = 20) -> list[str]:
    lines = meaningful_lines(text)
    return lines[: max(max_lines, 0)]


def compact_payload_keys(payload: dict[str, Any]) -> list[str]:
    return sorted(str(key) for key in payload.keys())
