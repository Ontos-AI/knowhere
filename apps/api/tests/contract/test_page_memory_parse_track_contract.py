from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.core.exceptions.domain_exceptions import ValidationException


def test_page_memory_parse_track_rejects_when_flag_disabled(monkeypatch) -> None:
    from app.services.document_ingestion.service import (
        _validate_parse_track_for_extension,
    )
    from shared.core.config import settings

    monkeypatch.setattr(
        settings,
        "RETRIEVAL_PAGE_MEMORY_ENABLED",
        False,
    )

    with pytest.raises(ValidationException):
        _validate_parse_track_for_extension(
            parse_track="page_memory",
            file_extension=".pdf",
        )


def test_page_memory_parse_track_allows_only_pdf_and_pptx(monkeypatch) -> None:
    from app.services.document_ingestion.service import (
        _validate_parse_track_for_extension,
    )
    from shared.core.config import settings

    monkeypatch.setattr(
        settings,
        "RETRIEVAL_PAGE_MEMORY_ENABLED",
        True,
    )

    _validate_parse_track_for_extension(
        parse_track="page_memory",
        file_extension=".pdf",
    )
    _validate_parse_track_for_extension(
        parse_track="page_memory",
        file_extension=".pptx",
    )
    with pytest.raises(ValidationException):
        _validate_parse_track_for_extension(
            parse_track="page_memory",
            file_extension=".docx",
        )
