"""PDF MinerU provider dispatcher with cloud-preserving defaults."""

from __future__ import annotations

from app.services.document_parser.providers.mineru.local_pdf_service import (
    parse_via_local,
)
from app.services.document_parser.providers.mineru.pdf_service import parse_via_full
from shared.core.config import settings


def parse_pdf(
    pdf_path: str,
    filename: str,
    output_dir: str,
    *,
    s3_key: str | None = None,
) -> None:
    """Parse a PDF through the configured provider without silent fallback."""

    if settings.MINERU_PROVIDER == "cloud":
        parse_via_full(pdf_path, filename, output_dir, s3_key=s3_key)
        return
    if settings.MINERU_PROVIDER == "local":
        parse_via_local(pdf_path, filename, output_dir, s3_key=s3_key)
        return
    raise ValueError(f"Unsupported MinerU provider: {settings.MINERU_PROVIDER!r}")
