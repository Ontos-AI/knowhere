from __future__ import annotations

import os

import pandas as pd

from app.services.document_parser.providers.mineru.pdf_service import (
    get_existing_mineru_source_s3_key,
)
from app.services.document_parser.formats.pdf.parser import parse_pdfs
from app.services.document_parser.formats.pdf.pptx_rendering import render_pdf_to_image_pdf
from loguru import logger

from shared.core.config import settings
from shared.services.storage.job_file_storage import JobFileStorage

RENDERED_PDF_TEMP_FILENAME = "_pptx_tmp.pdf"


def build_rendered_pdf_s3_key(job_id: str | None) -> str | None:
    """Store rendered parser artifacts under a stable transform/ prefix."""
    if settings.ENVIRONMENT == "development" or not job_id:
        return None
    return f"transform/{job_id}.rendered.pdf"


def parse_cached_rendered_pdf(
    *,
    rendered_pdf_s3_key: str | None,
    filename: str,
    output_dir: str,
    base_llm_paras: dict[str, object],
    relative_root: str | None,
) -> pd.DataFrame | None:
    """Parse a previously rendered PDF from S3 without re-reading the source deck."""
    if rendered_pdf_s3_key is None:
        return None

    cached_rendered_pdf_s3_key = get_existing_mineru_source_s3_key(rendered_pdf_s3_key)
    if cached_rendered_pdf_s3_key is None:
        return None

    logger.info(
        f"[rendered_pdf_transform] Reusing rendered PDF for MinerU URL mode: {rendered_pdf_s3_key}"
    )
    cached_rendered_pdf_path = JobFileStorage().download_upload_to_temp(
        cached_rendered_pdf_s3_key,
        suffix=".pdf",
        temp_dir=output_dir,
    )
    try:
        return parse_pdfs(
            cached_rendered_pdf_path,
            filename,
            output_dir,
            base_llm_paras,
            relative_root=relative_root,
            s3_key=cached_rendered_pdf_s3_key,
        )
    finally:
        if os.path.exists(cached_rendered_pdf_path):
            os.remove(cached_rendered_pdf_path)


def parse_rendered_pdf_bytes(
    *,
    pdf_bytes: bytes,
    filename: str,
    output_dir: str,
    base_llm_paras: dict[str, object],
    relative_root: str | None,
    rendered_pdf_s3_key: str | None = None,
) -> pd.DataFrame:
    image_only_pdf_bytes = render_pdf_to_image_pdf(pdf_bytes)
    temporary_pdf_path = os.path.join(output_dir, RENDERED_PDF_TEMP_FILENAME)
    with open(temporary_pdf_path, "wb") as temporary_pdf_file:
        temporary_pdf_file.write(image_only_pdf_bytes)

    try:
        return parse_pdfs(
            temporary_pdf_path,
            filename=filename,
            output_dir=output_dir,
            base_llm_paras=base_llm_paras,
            relative_root=relative_root,
            s3_key=rendered_pdf_s3_key,
        )
    finally:
        if os.path.exists(temporary_pdf_path):
            os.remove(temporary_pdf_path)
