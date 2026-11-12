from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from app.services.document_parser.orchestration.format_adapters import (
    MediaParseAdapter,
)
from app.services.document_ingestion.file_size_policy import max_source_file_bytes
from app.services.document_parser.orchestration.format_router import (
    MEDIA_SOURCE_EXTENSIONS,
    DocumentFormat,
    get_document_parse_adapter,
    resolve_document_format,
)
from shared.services.http.url_file_type import CONTENT_TYPE_TO_EXTENSION
from shared.services.storage.job_file_storage import JobFileStorage


@pytest.mark.parametrize("extension", MEDIA_SOURCE_EXTENSIONS)
def test_media_source_extensions_route_to_media_parse(extension: str) -> None:
    assert resolve_document_format(f"/tmp/talk{extension}") == DocumentFormat.MEDIA


@pytest.mark.parametrize("extension", (".srt", ".vtt"))
def test_subtitle_extensions_are_not_routed_until_parser_exists(extension: str) -> None:
    from shared.core.exceptions.domain_exceptions import ValidationException

    with pytest.raises(ValidationException):
        resolve_document_format(f"/tmp/captions{extension}")


def test_media_adapter_is_registered() -> None:
    media_adapter = get_document_parse_adapter(DocumentFormat.MEDIA)
    assert isinstance(media_adapter, MediaParseAdapter)


@pytest.mark.parametrize(
    ("extension", "content_type"),
    [
        (".mp3", "audio/mpeg"),
        (".wav", "audio/wav"),
        (".m4a", "audio/mp4"),
        (".mp4", "video/mp4"),
        (".mov", "video/quicktime"),
    ],
)
def test_media_upload_content_types(extension: str, content_type: str) -> None:
    assert JobFileStorage.get_content_type(extension) == content_type
    assert CONTENT_TYPE_TO_EXTENSION[content_type] == extension


@pytest.mark.parametrize("extension", MEDIA_SOURCE_EXTENSIONS)
def test_media_sources_use_media_file_size_cap(
    monkeypatch: pytest.MonkeyPatch,
    extension: str,
) -> None:
    # Patch the settings object the live function reads: contract fixtures may
    # reload app modules after this file imported them.
    settings = max_source_file_bytes.__globals__["settings"]
    monkeypatch.setattr(settings, "MAX_FILE_SIZE", 10)
    monkeypatch.setattr(settings, "MAX_MEDIA_FILE_SIZE", 20)
    assert max_source_file_bytes(file_extension=extension) == 20


def test_document_sources_keep_document_file_size_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patch the settings object the live function reads: contract fixtures may
    # reload app modules after this file imported them.
    settings = max_source_file_bytes.__globals__["settings"]
    monkeypatch.setattr(settings, "MAX_FILE_SIZE", 10)
    monkeypatch.setattr(settings, "MAX_MEDIA_FILE_SIZE", 20)
    assert max_source_file_bytes(file_extension=".pdf") == 10
