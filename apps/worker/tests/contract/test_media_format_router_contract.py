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
    SubtitleParseAdapter,
)
from app.services.document_ingestion.file_size_policy import max_source_file_bytes
from app.services.document_parser.orchestration.format_router import (
    MEDIA_SOURCE_EXTENSIONS,
    SUBTITLE_SOURCE_EXTENSIONS,
    DocumentFormat,
    get_document_parse_adapter,
    resolve_document_format,
)
from shared.services.http.url_file_type import CONTENT_TYPE_TO_EXTENSION
from shared.services.jobs.progressive_stages import required_stages_before_finalization
from shared.services.storage.job_file_storage import JobFileStorage


@pytest.mark.parametrize("extension", MEDIA_SOURCE_EXTENSIONS)
def test_media_source_extensions_route_to_media_parse(extension: str) -> None:
    assert resolve_document_format(f"/tmp/talk{extension}") == DocumentFormat.MEDIA


@pytest.mark.parametrize("extension", SUBTITLE_SOURCE_EXTENSIONS)
def test_subtitle_source_extensions_route_to_subtitle_parse(extension: str) -> None:
    assert resolve_document_format(f"/tmp/captions{extension}") == DocumentFormat.SUBTITLE


def test_media_and_subtitle_adapters_are_registered() -> None:
    media_adapter = get_document_parse_adapter(DocumentFormat.MEDIA)
    subtitle_adapter = get_document_parse_adapter(DocumentFormat.SUBTITLE)
    assert isinstance(media_adapter, MediaParseAdapter)
    assert isinstance(subtitle_adapter, SubtitleParseAdapter)


@pytest.mark.parametrize(
    ("extension", "content_type"),
    [
        (".mp3", "audio/mpeg"),
        (".wav", "audio/wav"),
        (".m4a", "audio/mp4"),
        (".mp4", "video/mp4"),
        (".mov", "video/quicktime"),
        (".srt", "application/x-subrip"),
        (".vtt", "text/vtt"),
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
    from shared.core.config import settings

    monkeypatch.setattr(settings, "MAX_FILE_SIZE", 10)
    monkeypatch.setattr(settings, "MAX_MEDIA_FILE_SIZE", 20)
    assert max_source_file_bytes(file_extension=extension) == 20


def test_subtitle_and_document_sources_keep_document_file_size_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core.config import settings

    monkeypatch.setattr(settings, "MAX_FILE_SIZE", 10)
    monkeypatch.setattr(settings, "MAX_MEDIA_FILE_SIZE", 20)
    assert max_source_file_bytes(file_extension=".srt") == 10
    assert max_source_file_bytes(file_extension=".pdf") == 10


def test_media_progressive_stages_match_sheet_empty_revision_map() -> None:
    assert required_stages_before_finalization("media") == ()
    assert required_stages_before_finalization("sheet") == ()
