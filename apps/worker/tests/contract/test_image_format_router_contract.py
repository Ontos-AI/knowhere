from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from app.services.document_ingestion.page_estimator import PageEstimator
from app.services.document_parser.orchestration.format_router import (
    IMAGE_SOURCE_EXTENSIONS,
    DocumentFormat,
    resolve_document_format,
)
from shared.core.exceptions.domain_exceptions import ValidationException


@pytest.mark.parametrize("extension", IMAGE_SOURCE_EXTENSIONS)
def test_image_source_extensions_route_to_image_parse(extension: str) -> None:
    assert resolve_document_format(f"/tmp/upload{extension}") == DocumentFormat.IMAGE


def test_svg_is_rejected_by_parse_router() -> None:
    with pytest.raises(ValidationException) as exc_info:
        resolve_document_format("/tmp/upload.svg")
    assert exc_info.value.user_message == "Unsupported file type: .svg"


@pytest.mark.parametrize("extension", IMAGE_SOURCE_EXTENSIONS)
def test_image_source_extensions_bill_as_one_page(
    tmp_path: Path,
    extension: str,
) -> None:
    source = tmp_path / f"upload{extension}"
    source.write_bytes(b"image-bytes")
    estimate = PageEstimator.estimate_workload(str(source))
    assert estimate.page_count == 1
    assert estimate.method == "image_default"
