"""DOCX progressive baseline: LibreOffice pagination + PyMuPDF page text."""

from __future__ import annotations

import gc
import json
import os
import shutil
from pathlib import Path

import pandas as pd
from pandas import Index

from app.services.document_agent.pdf_text import coerce_page_text_cache
from app.services.document_parser.conversion.legacy_converter import (
    _convert_with_libreoffice,
)
from app.services.document_parser.formats.pdf.pymupdf_subprocess import (
    run_in_child_process,
    worker,
)
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.support.parser_rows import PARSER_DF_COLUMNS, ParsedRow
from shared.services.chunks.path_segments import join_document_path

RENDERED_PDF_REL = "source.pdf"
PAGE_TEXT_CACHE_REL = "page_text_cache.json"


@worker
def _read_rendered_pdf_texts_worker(queue, pdf_path: str) -> None:
    import pymupdf  # type: ignore[import]

    texts: dict[int, str] = {}
    document = None
    try:
        document = pymupdf.open(pdf_path)
        for index in range(document.page_count):
            texts[index + 1] = str(document[index].get_text() or "")
    finally:
        if document is not None:
            try:
                document.close()
            except Exception as exc:
                print(f"Failed to close rendered PDF in DOCX baseline: {exc}")
        gc.collect()
    queue.put({"ok": True, "texts": texts})


def parse_docx_baseline(
    docx_path: str,
    output_dir: str,
    *,
    filename: str,
) -> pd.DataFrame:
    """Render DOCX to PDF once, emit one type=page row per physical page."""
    os.makedirs(output_dir, exist_ok=True)
    rendered_pdf_path, _rendered_name = _convert_with_libreoffice(
        source_path=docx_path,
        outdir=output_dir,
        convert_to_arg="pdf:writer_pdf_Export",
        expected_output_ext="pdf",
        operation="convert_docx_to_pdf",
    )
    persisted_pdf_path = os.path.join(output_dir, RENDERED_PDF_REL)
    if Path(rendered_pdf_path).resolve() != Path(persisted_pdf_path).resolve():
        shutil.copyfile(rendered_pdf_path, persisted_pdf_path)
    page_texts = _read_rendered_page_texts(persisted_pdf_path)
    write_page_text_cache(output_dir, page_texts)
    return _build_page_rows(filename=filename, page_texts=page_texts)


def write_page_text_cache(output_dir: str, page_texts: dict[int, str]) -> str:
    cache_path = os.path.join(output_dir, PAGE_TEXT_CACHE_REL)
    payload = {
        str(page): text
        for page, text in sorted(page_texts.items(), key=lambda item: item[0])
    }
    with open(cache_path, "w", encoding="utf-8") as cache_file:
        json.dump(payload, cache_file, ensure_ascii=False)
    return cache_path


def load_page_text_cache(output_dir: str) -> dict[int, str]:
    cache_path = os.path.join(output_dir, PAGE_TEXT_CACHE_REL)
    with open(cache_path, encoding="utf-8") as cache_file:
        raw = json.load(cache_file)
    return coerce_page_text_cache(raw)


def _read_rendered_page_texts(pdf_path: str) -> dict[int, str]:
    result = run_in_child_process(_read_rendered_pdf_texts_worker, pdf_path)
    return {
        int(page): str(text or "")
        for page, text in (result.get("texts") or {}).items()
    }


def _build_page_rows(*, filename: str, page_texts: dict[int, str]) -> pd.DataFrame:
    timestamp = get_str_time()
    pages = sorted(page_texts)
    rows = []
    for page in pages:
        content = str(page_texts.get(page, "") or "").strip()
        path = join_document_path([filename, f"p{page}"])
        rows.append(
            ParsedRow(
                content=content,
                path=path,
                type="page",
                know_id=f"node_{gen_str_codes(f'{filename}::{path}')}",
                addtime=timestamp,
                page_nums=str(page),
            ).to_dict()
        )
    return pd.DataFrame(rows, columns=Index(PARSER_DF_COLUMNS))
