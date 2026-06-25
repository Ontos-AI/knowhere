from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from app.services.document_parser.assets.inline_asset import build_table_asset_row  # noqa: E402
from app.services.document_parser.tables.table_asset_writer import (  # noqa: E402
    TableAssetInput,
    write_table_asset,
)


def test_table_asset_writer_stores_reference_not_html_content(tmp_path) -> None:
    html = "<table><tr><td>SHOULD NOT LEAK</td></tr></table>"

    row = write_table_asset(
        TableAssetInput(
            html=html,
            output_dir=str(tmp_path),
            table_name="table-1.html",
            summary="table summary",
            keywords="name;value",
            know_id="table-1",
            addtime="2026-06-24 00:00:00",
        )
    )

    assert row.content == "tables/table-1.html"
    assert "<table" not in row.content
    assert (tmp_path / "tables" / "table-1.html").read_text(encoding="utf-8") == html


def test_inline_table_asset_row_stores_reference_not_html_content() -> None:
    row = build_table_asset_row(
        relative_path="tables/table-2.html",
        summary="table summary",
        keywords="name;value",
        know_id="table-2",
        addtime="2026-06-24 00:00:00",
    )

    assert row.content == "tables/table-2.html"
    assert "<table" not in row.content
