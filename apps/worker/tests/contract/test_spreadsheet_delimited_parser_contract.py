from __future__ import annotations

import os
from pathlib import Path

import pytest
from pytest import MonkeyPatch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")


@pytest.fixture(autouse=True)
def _fail_closed_spreadsheet_headers(
    monkeypatch: MonkeyPatch,
    worker_contract_environment: None,
) -> None:
    del worker_contract_environment
    monkeypatch.setattr(
        "app.services.document_parser.formats.excel.header_detect."
        "TextClientSpreadsheetHeaderDetector.detect_spreadsheet_headers",
        lambda self, rows_text: None,
    )
    monkeypatch.setattr(
        "app.services.document_parser.formats.excel.header_detect."
        "TextClientSpreadsheetHeaderDetector.locate_header_start",
        lambda self, rows_text: None,
    )
    monkeypatch.setattr(
        "app.services.document_parser.support.choice_judge."
        "GatewayLunaJudge.choose",
        lambda self, **kwargs: None,
    )


def _parse_table_source(
    *,
    source_path: Path,
    filename: str,
    output_root: Path,
):
    from app.services.document_parser.parse_service import checkerboard_parse_output

    return checkerboard_parse_output(
        file_full_path=str(source_path),
        filename=filename,
        output_dir=str(output_root),
        internal_output_filename=filename,
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )


def _assert_single_table_chunk(parse_output, *, filename: str, sheet_title: str) -> str:
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    assert len(parsed_df) == 1
    assert parsed_df["type"].iloc[0].startswith("table")
    assert parsed_df["path"].tolist() == [f"{filename}/{sheet_title}"]
    assert str(parsed_df["content"].iloc[0]).startswith("tables/")
    table_html = (
        Path(parse_output.output_dir) / parsed_df["content"].iloc[0]
    ).read_text(encoding="utf-8")
    work_xlsx = Path(parse_output.output_dir) / ".work" / f"{Path(filename).stem}.xlsx"
    assert work_xlsx.is_file()
    return table_html


def test_utf8_sig_csv_parses_to_table_html(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    source_path = tmp_path / "budget.csv"
    source_path.write_bytes("Region,Value\nNorth,10\n".encode("utf-8-sig"))

    parse_output = _parse_table_source(
        source_path=source_path,
        filename="budget.csv",
        output_root=tmp_path / "parser-output",
    )
    table_html = _assert_single_table_chunk(
        parse_output,
        filename="budget.csv",
        sheet_title="budget",
    )
    assert "Region" in table_html
    assert "North" in table_html
    assert "10" in table_html


def test_gb18030_csv_parses_to_table_html(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    source_path = tmp_path / "products.csv"
    source_path.write_bytes("产品,数量\n茶叶,2\n".encode("gb18030"))

    parse_output = _parse_table_source(
        source_path=source_path,
        filename="products.csv",
        output_root=tmp_path / "parser-output",
    )
    table_html = _assert_single_table_chunk(
        parse_output,
        filename="products.csv",
        sheet_title="products",
    )
    assert "产品" in table_html
    assert "茶叶" in table_html


def test_quoted_newline_csv_parses_to_table_html(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    source_path = tmp_path / "notes.csv"
    source_path.write_text('title,note\n"hello\nworld",ok\n', encoding="utf-8")

    parse_output = _parse_table_source(
        source_path=source_path,
        filename="notes.csv",
        output_root=tmp_path / "parser-output",
    )
    table_html = _assert_single_table_chunk(
        parse_output,
        filename="notes.csv",
        sheet_title="notes",
    )
    assert "hello" in table_html
    assert "world" in table_html
    assert "ok" in table_html


def test_tsv_tab_parses_to_table_html(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    source_path = tmp_path / "budget.tsv"
    source_path.write_text("Region\tValue\nNorth\t10\n", encoding="utf-8")

    parse_output = _parse_table_source(
        source_path=source_path,
        filename="budget.tsv",
        output_root=tmp_path / "parser-output",
    )
    table_html = _assert_single_table_chunk(
        parse_output,
        filename="budget.tsv",
        sheet_title="budget",
    )
    assert "Region" in table_html
    assert "North" in table_html
    assert "10" in table_html
