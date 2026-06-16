from __future__ import annotations

import os

from app.services.document_parser.formats.pptx.parser import (
    _pptx_bytes_to_pdf_bytes,
    pptx_to_pdf_libreoffice,
)
from app.services.common.file_loading import load_file_bytes
from loguru import logger

from shared.core.exceptions.domain_exceptions import ValidationException


def normalize_to_pdf(
    *,
    file_path: str,
    filename: str,
    output_dir: str,
    base_url: str = "",
) -> tuple[str, str]:
    """Return a local PDF path and filename for page-memory processing."""
    extension = os.path.splitext(filename)[1].lower()
    if extension == ".pdf":
        return file_path, filename
    if extension == ".pptx":
        return _normalize_pptx_to_pdf(
            file_path=file_path,
            filename=filename,
            output_dir=output_dir,
            base_url=base_url,
        )
    raise ValidationException(
        user_message="page_memory parse track only supports PDF and PPTX",
        violations=[
            {
                "field": "parse_track",
                "description": "Allowed file types in this build: .pdf, .pptx",
            }
        ],
    )


def _normalize_pptx_to_pdf(
    *,
    file_path: str,
    filename: str,
    output_dir: str,
    base_url: str,
) -> tuple[str, str]:
    pptx_data = load_file_bytes(file_path, file_url=base_url)
    pdf_filename = f"{os.path.splitext(filename)[0]}.pdf"
    pdf_path = os.path.join(output_dir, pdf_filename)
    try:
        pdf_bytes = _pptx_bytes_to_pdf_bytes(pptx_data, filename)
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        return pdf_path, pdf_filename
    except Exception as exc:
        logger.warning(
            "[page_memory] iLoveAPI PPTX normalization failed for {}; "
            "falling back to LibreOffice: {}",
            filename,
            exc,
        )
        local_pptx_path = os.path.join(output_dir, filename)
        with open(local_pptx_path, "wb") as f:
            f.write(pptx_data)
        return pptx_to_pdf_libreoffice(local_pptx_path, output_dir)
