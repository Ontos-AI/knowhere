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

from app.services.document_parser.orchestration.format_router import (
    TABLE_SOURCE_EXTENSIONS,
    DocumentFormat,
    resolve_document_format,
)
from app.services.document_parser.orchestration.office_container_validator import (
    validate_office_container,
)


@pytest.mark.parametrize("extension", TABLE_SOURCE_EXTENSIONS)
def test_table_source_extensions_route_to_spreadsheet_parse(extension: str) -> None:
    assert (
        resolve_document_format(f"/tmp/upload{extension}")
        == DocumentFormat.SPREADSHEET
    )


def test_csv_skips_office_zip_container_validation(tmp_path: Path) -> None:
    source = tmp_path / "budget.csv"
    source.write_text("Region,Value\nNorth,10\n", encoding="utf-8")
    validate_office_container(str(source))
