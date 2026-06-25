from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from loguru import logger

from shared.services.storage.result_storage import JobResultStorage

_SOURCE_PDF_REF = "source.pdf"
_PAGE_PDF_DIR = "page_pdfs"


def crop_source_pdf_pages(
    *,
    job_id: str,
    pages: list[int],
    storage: JobResultStorage | None = None,
    expires_in: int = 3600,
    temp_dir: str | None = None,
) -> str | None:
    normalized_pages = _normalize_pages(pages)
    if not job_id or not normalized_pages:
        return None

    result_storage = storage or JobResultStorage()
    cache_ref = _cache_ref(normalized_pages)
    if result_storage.verify_raw_exists(job_id=job_id, relative_path=cache_ref):
        return result_storage.generate_artifact_url(
            job_id=job_id,
            artifact_ref=cache_ref,
            expires_in=expires_in,
        )

    if not result_storage.verify_raw_exists(job_id=job_id, relative_path=_SOURCE_PDF_REF):
        logger.warning("[page_pdf_crop] source.pdf missing for job_id={}", job_id)
        return None

    work_dir = temp_dir or tempfile.gettempdir()
    try:
        with tempfile.TemporaryDirectory(dir=work_dir) as local_dir:
            source_path = result_storage.download_raw_to_temp(
                job_id=job_id,
                relative_path=_SOURCE_PDF_REF,
                suffix=".pdf",
                temp_dir=local_dir,
            )
            cropped_path = os.path.join(local_dir, Path(cache_ref).name)
            _write_cropped_pdf(
                source_path=source_path,
                output_path=cropped_path,
                pages=normalized_pages,
            )
            result_storage.upload_raw_file(
                job_id=job_id,
                relative_path=cache_ref,
                local_file_path=cropped_path,
            )
    except Exception as exc:
        logger.warning(
            "[page_pdf_crop] failed to crop source PDF for job_id={}, pages={}: {}",
            job_id,
            normalized_pages,
            exc,
        )
        return None

    return result_storage.generate_artifact_url(
        job_id=job_id,
        artifact_ref=cache_ref,
        expires_in=expires_in,
    )


def _normalize_pages(pages: list[int]) -> list[int]:
    normalized: set[int] = set()
    for page in pages:
        try:
            value = int(page)
        except (TypeError, ValueError):
            continue
        if value > 0:
            normalized.add(value)
    return sorted(normalized)


def _cache_ref(pages: list[int]) -> str:
    digest = hashlib.sha1(",".join(str(page) for page in pages).encode("utf-8")).hexdigest()
    return f"{_PAGE_PDF_DIR}/{digest}.pdf"


def _write_cropped_pdf(*, source_path: str, output_path: str, pages: list[int]) -> None:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(source_path)
    writer = PdfWriter()
    page_count = len(reader.pages)
    selected_pages = [page for page in pages if 1 <= page <= page_count]
    if not selected_pages:
        raise ValueError("No requested pages exist in source PDF")
    for page in selected_pages:
        writer.add_page(reader.pages[page - 1])
    with open(output_path, "wb") as file_obj:
        writer.write(file_obj)
