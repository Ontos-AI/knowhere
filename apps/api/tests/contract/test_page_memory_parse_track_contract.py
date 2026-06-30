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
from shared.models.schemas.job import JobCreate


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


def test_implicit_parse_track_defaults_to_page_memory_for_supported_files(
    monkeypatch,
) -> None:
    from app.services.document_ingestion.service import _apply_effective_parse_track
    from shared.core.config import settings

    monkeypatch.setattr(
        settings,
        "RETRIEVAL_PAGE_MEMORY_ENABLED",
        True,
    )
    payload = JobCreate(source_type="file", file_name="policy.pdf")

    effective = _apply_effective_parse_track(payload, file_extension=".pdf")

    assert effective == "page_memory"
    assert payload.parse_track == "page_memory"


def test_implicit_parse_track_falls_back_to_chunk_for_unsupported_files(
    monkeypatch,
) -> None:
    from app.services.document_ingestion.service import _apply_effective_parse_track
    from shared.core.config import settings

    monkeypatch.setattr(
        settings,
        "RETRIEVAL_PAGE_MEMORY_ENABLED",
        True,
    )
    payload = JobCreate(source_type="file", file_name="policy.docx")

    effective = _apply_effective_parse_track(payload, file_extension=".docx")

    assert effective == "chunk"
    assert payload.parse_track == "chunk"


def test_implicit_parse_track_falls_back_to_chunk_when_flag_disabled(
    monkeypatch,
) -> None:
    from app.services.document_ingestion.service import _apply_effective_parse_track
    from shared.core.config import settings

    monkeypatch.setattr(
        settings,
        "RETRIEVAL_PAGE_MEMORY_ENABLED",
        False,
    )
    payload = JobCreate(source_type="file", file_name="policy.pdf")

    effective = _apply_effective_parse_track(payload, file_extension=".pdf")

    assert effective == "chunk"
    assert payload.parse_track == "chunk"


def test_explicit_page_memory_still_rejects_unsupported_files(monkeypatch) -> None:
    from app.services.document_ingestion.service import _apply_effective_parse_track
    from shared.core.config import settings

    monkeypatch.setattr(
        settings,
        "RETRIEVAL_PAGE_MEMORY_ENABLED",
        True,
    )
    payload = JobCreate(
        source_type="file",
        file_name="policy.docx",
        parse_track="page_memory",
    )

    with pytest.raises(ValidationException):
        _apply_effective_parse_track(payload, file_extension=".docx")
