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


def _table_cells(table_html: str) -> list[str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(table_html, "html.parser")
    return [cell.get_text(strip=True) for cell in soup.find_all(["th", "td"])]


def _parse_single_table(tmp_path: Path, filename: str, data: bytes, sheet_title: str) -> str:
    source_path = tmp_path / filename
    source_path.write_bytes(data)
    parse_output = _parse_table_source(
        source_path=source_path,
        filename=filename,
        output_root=tmp_path / "parser-output",
    )
    return _assert_single_table_chunk(
        parse_output,
        filename=filename,
        sheet_title=sheet_title,
    )


def test_single_column_csv_parses_as_one_column(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    table_html = _parse_single_table(tmp_path, "names.csv", b"name\nalice\nbob\n", "names")
    assert _table_cells(table_html) == ["name", "alice", "bob"]


def test_semicolon_csv_detects_its_delimiter(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    table_html = _parse_single_table(
        tmp_path, "eu.csv", b"Region;Value\nNorth;10\nSouth;20\n", "eu"
    )
    assert _table_cells(table_html) == ["Region", "Value", "North", "10", "South", "20"]


def test_csv_filename_with_sheet_title_characters_parses(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    table_html = _parse_single_table(
        tmp_path, "report[2024].csv", b"a,b\nx,y\n", "report_2024_"
    )
    assert _table_cells(table_html) == ["a", "b", "x", "y"]


def test_csv_numbers_keep_their_written_form(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    table_html = _parse_single_table(
        tmp_path,
        "numbers.csv",
        b"id,amt,phone,sci,padded,plain\n001,1.10,+8613800000000,1e5, 12 ,3.5\n",
        "numbers",
    )
    assert _table_cells(table_html) == [
        "id", "amt", "phone", "sci", "padded", "plain",
        "001", "1.10", "+8613800000000", "1e5", "12", "3.5",
    ]


def test_short_gb18030_csv_is_not_read_as_utf16(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    data = "名名,数量\n".encode("gb18030")
    assert data.decode("utf-16")  # these bytes are also valid UTF-16 without a BOM
    table_html = _parse_single_table(tmp_path, "short.csv", data, "short")
    assert _table_cells(table_html) == ["名名", "数量"]


def test_utf16_csv_with_bom_parses(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    del worker_contract_environment
    table_html = _parse_single_table(
        tmp_path, "wide.csv", "产品,数量\n茶叶,2\n".encode("utf-16"), "wide"
    )
    assert _table_cells(table_html) == ["产品", "数量", "茶叶", "2"]
