from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pytest import MonkeyPatch

_HEADER_DETECT = (
    "app.services.document_parser.formats.excel.header_detect."
    "TextClientSpreadsheetHeaderDetector.detect_spreadsheet_headers"
)


@pytest.fixture(autouse=True)
def _fail_closed_spreadsheet_headers(
    monkeypatch: MonkeyPatch,
    worker_contract_environment: None,
) -> None:
    del worker_contract_environment
    monkeypatch.setattr(
        _HEADER_DETECT,
        lambda self, rows_text, rowspan_hint: None,
    )


def _last_row_from_rows_text(rows_text: str) -> int:
    return max(
        sum(1 for line in rows_text.splitlines() if line.startswith("Row ")) - 1,
        0,
    )


def _parse_xlsx(workbook_path: Path, output_root: Path, filename: str):
    from app.services.document_parser.parse_service import checkerboard_parse_output

    return checkerboard_parse_output(
        file_full_path=str(workbook_path),
        filename=filename,
        output_dir=str(output_root),
        internal_output_filename=filename,
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )


def _extra_metadata(parsed_df) -> dict:
    raw = parsed_df["extra_metadata"].iloc[0]
    if isinstance(raw, dict):
        return raw
    return json.loads(str(raw))


def _write_contract_workbook(workbook_path: Path) -> None:
    import openpyxl

    workbook = openpyxl.Workbook()
    visible_sheet = workbook.active
    visible_sheet.title = "Visible"
    visible_sheet["A1"] = "Region"
    visible_sheet["B1"] = "Value"
    visible_sheet["A2"] = "North"
    visible_sheet["B2"] = 10

    hidden_sheet = workbook.create_sheet("Hidden")
    hidden_sheet.sheet_state = "hidden"
    hidden_sheet["A1"] = "Secret"
    hidden_sheet["B1"] = "Value"
    hidden_sheet["A2"] = "Hidden"
    hidden_sheet["B2"] = 99

    workbook.save(workbook_path)


def test_xlsx_parser_contract_uses_stable_entrypoint_and_ignores_hidden_sheets(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    from app.services.document_parser.parse_service import checkerboard_parse_output

    workbook_path = tmp_path / "budget.xlsx"
    output_root = tmp_path / "parser-output"
    _write_contract_workbook(workbook_path)

    parse_output = checkerboard_parse_output(
        file_full_path=str(workbook_path),
        filename="budget.xlsx",
        output_dir=str(output_root),
        internal_output_filename="budget.xlsx",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )

    full_output_dir = parse_output.output_dir
    parsed_df = parse_output.parsed_df
    assert full_output_dir.endswith("budget.xlsx")
    assert parsed_df is not None
    assert parsed_df["type"].iloc[0].startswith("table")
    assert "[tables/table-[1] Visible.html]" in parsed_df["type"].iloc[0]
    assert parsed_df["path"].tolist() == ["budget.xlsx/Visible"]
    assert parsed_df["summary"].tolist() == ["table-[1]"]
    assert parsed_df["content"].iloc[0] == "tables/table-[1] Visible.html"
    assert "Region" in parsed_df["keywords"].iloc[0]
    assert "Value" in parsed_df["keywords"].iloc[0]

    metadata = _extra_metadata(parsed_df)
    assert metadata["header_row_count"] == 1
    assert metadata["row_header_col_count"] == 0
    assert metadata["column_headers"] == ["Region", "Value"]
    assert metadata["sheet_name"] == "Visible"
    assert metadata["excel_range"] == "A1:B2"

    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks

    chunks = dataframe_to_chunks(parsed_df)
    assert chunks[0]["metadata"]["column_headers"] == ["Region", "Value"]
    assert chunks[0]["metadata"]["header_row_count"] == 1

    table_html = Path(full_output_dir) / "tables" / "table-[1] Visible.html"
    table_html_text = table_html.read_text(encoding="utf-8")

    assert "North" in table_html_text
    assert "10" in table_html_text
    assert "Secret" not in table_html_text
    assert "Hidden" not in table_html_text


def test_parser_maps_document_name_to_task_local_path_segment(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    from app.services.document_parser.parse_service import checkerboard_parse_output

    workbook_path = tmp_path / "budget.xlsx"
    output_root = tmp_path / "parser-output"
    _write_contract_workbook(workbook_path)

    parse_output = checkerboard_parse_output(
        file_full_path=str(workbook_path),
        filename="/tmp/../images.xlsx",
        output_dir=str(output_root),
        internal_output_filename="../../images.xlsx",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )

    full_output_dir = parse_output.output_dir
    parsed_df = parse_output.parsed_df
    assert (
        os.path.commonpath([str(output_root.resolve()), full_output_dir])
        == str(output_root.resolve())
    )
    assert full_output_dir.endswith("images.xlsx")
    assert parsed_df is not None
    assert parsed_df["path"].tolist() == ["images.xlsx/Visible"]


def _write_workbook_with_sparse_datetimes(workbook_path: Path) -> None:
    """Datetime column with blanks → pandas NaT in-frame; HTML must still render."""
    import openpyxl
    from datetime import datetime

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Roster"
    sheet["A1"] = "Name"
    sheet["B1"] = "SignupDate"
    sheet["A2"] = "Alice"
    sheet["B2"] = datetime(2026, 6, 27)
    sheet["A3"] = "Bob"
    sheet["B3"] = None
    sheet["A4"] = "Carol"
    sheet["B4"] = datetime(2026, 7, 4)
    workbook.save(workbook_path)


def test_xlsx_parser_tolerates_nat_in_datetime_column(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    from app.services.document_parser.parse_service import checkerboard_parse_output

    workbook_path = tmp_path / "roster.xlsx"
    output_root = tmp_path / "parser-output"
    _write_workbook_with_sparse_datetimes(workbook_path)

    parse_output = checkerboard_parse_output(
        file_full_path=str(workbook_path),
        filename="roster.xlsx",
        output_dir=str(output_root),
        internal_output_filename="roster.xlsx",
        summary_image=False,
        summary_table=False,
        summary_txt=False,
        smart_title_parse=False,
        stopwords=[],
    )

    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    assert len(parsed_df) == 1

    table_html = (
        Path(parse_output.output_dir) / "tables" / "table-[1] Roster.html"
    ).read_text(encoding="utf-8")
    assert "Alice" in table_html
    assert "2026-06-27" in table_html
    assert "Carol" in table_html
    assert "2026-07-04" in table_html
    assert "NaT" not in table_html


def test_xlsx_numeric_year_is_a_column_header(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    import openpyxl

    del worker_contract_environment
    workbook_path = tmp_path / "years.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Years"
    sheet["A1"] = 2024
    sheet["B1"] = "Amount"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    workbook.save(workbook_path)

    parse_output = _parse_xlsx(
        workbook_path, tmp_path / "parser-output", "years.xlsx"
    )
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    metadata = _extra_metadata(parsed_df)
    assert metadata["header_row_count"] == 1
    assert metadata["column_headers"] == ["2024", "Amount"]
    table_html = (
        Path(parse_output.output_dir) / parsed_df["content"].iloc[0]
    ).read_text(encoding="utf-8")
    assert "2024" in table_html
    assert "North" in table_html


def test_xlsx_multilevel_headers_flatten_and_feed_query_table(
    worker_contract_environment: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    import openpyxl

    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
    from shared.services.retrieval.hydration.table_grid import (
        grid_sql_rows,
        html_to_grid,
        sql_columns,
    )

    del worker_contract_environment
    monkeypatch.setattr(
        _HEADER_DETECT,
        lambda self, rows_text, rowspan_hint: (
            2,
            1,
            _last_row_from_rows_text(rows_text),
        ),
    )
    workbook_path = tmp_path / "nested.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Nested"
    sheet["A1"] = "Item"
    sheet.merge_cells("B1:C1")
    sheet["B1"] = 2024
    sheet["B2"] = "Q1"
    sheet["C2"] = "Q2"
    sheet["A3"] = "North"
    sheet["B3"] = 10
    sheet["C3"] = 20
    workbook.save(workbook_path)

    parse_output = _parse_xlsx(
        workbook_path, tmp_path / "parser-output", "nested.xlsx"
    )
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    metadata = _extra_metadata(parsed_df)
    assert metadata["header_row_count"] == 2
    assert metadata["row_header_col_count"] == 1
    assert metadata["column_headers"] == ["Item", "2024-->Q1", "2024-->Q2"]
    assert metadata["excel_range"] == "A1:C3"

    table_html = (
        Path(parse_output.output_dir) / parsed_df["content"].iloc[0]
    ).read_text(encoding="utf-8")
    assert "2024" in table_html
    assert "Q1" in table_html
    assert "North" in table_html

    chunks = dataframe_to_chunks(parsed_df)
    grid = html_to_grid(table_html)
    columns = sql_columns(grid, chunks[0]["metadata"])
    assert columns[0] == "row_header"
    assert "c_2024___Q1" in columns
    assert "c_2024___Q2" in columns
    assert grid_sql_rows(grid, chunks[0]["metadata"]) == [["North", "10", "20"]]


def test_xlsx_left_column_row_headers(
    worker_contract_environment: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    import openpyxl

    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
    from shared.services.retrieval.hydration.table_grid import (
        grid_sql_rows,
        html_to_grid,
        sql_columns,
    )

    del worker_contract_environment
    monkeypatch.setattr(
        _HEADER_DETECT,
        lambda self, rows_text, rowspan_hint: (
            1,
            1,
            _last_row_from_rows_text(rows_text),
        ),
    )
    workbook_path = tmp_path / "roster.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Scores"
    sheet["A1"] = "Name"
    sheet["B1"] = "Score"
    sheet["A2"] = "Alice"
    sheet["B2"] = 90
    workbook.save(workbook_path)

    parse_output = _parse_xlsx(
        workbook_path, tmp_path / "parser-output", "roster.xlsx"
    )
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    metadata = _extra_metadata(parsed_df)
    assert metadata["header_row_count"] == 1
    assert metadata["row_header_col_count"] == 1
    assert metadata["column_headers"] == ["Name", "Score"]
    table_html = (
        Path(parse_output.output_dir) / parsed_df["content"].iloc[0]
    ).read_text(encoding="utf-8")
    assert 'scope="row"' in table_html
    assert "Alice" in table_html
    chunks = dataframe_to_chunks(parsed_df)
    grid = html_to_grid(table_html)
    assert sql_columns(grid, chunks[0]["metadata"]) == ["row_header", "Score"]
    assert grid_sql_rows(grid, chunks[0]["metadata"]) == [["Alice", "90"]]


def test_xlsx_header_row_count_zero_still_emits_table(
    worker_contract_environment: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    import openpyxl

    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
    from shared.services.retrieval.hydration.table_grid import (
        grid_sql_rows,
        html_to_grid,
        sql_columns,
    )

    del worker_contract_environment
    monkeypatch.setattr(
        _HEADER_DETECT,
        lambda self, rows_text, rowspan_hint: (
            0,
            0,
            _last_row_from_rows_text(rows_text),
        ),
    )
    workbook_path = tmp_path / "records.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Records"
    sheet["A1"] = "Alice"
    sheet["B1"] = 90
    sheet["A2"] = "Bob"
    sheet["B2"] = 80
    workbook.save(workbook_path)

    parse_output = _parse_xlsx(
        workbook_path, tmp_path / "parser-output", "records.xlsx"
    )
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    metadata = _extra_metadata(parsed_df)
    assert metadata["header_row_count"] == 0
    assert metadata["row_header_col_count"] == 0
    assert metadata["column_headers"] == []
    table_html = (
        Path(parse_output.output_dir) / parsed_df["content"].iloc[0]
    ).read_text(encoding="utf-8")
    assert "Alice" in table_html
    assert "Bob" in table_html
    chunks = dataframe_to_chunks(parsed_df)
    grid = html_to_grid(table_html)
    assert sql_columns(grid, chunks[0]["metadata"]) == ["col_0", "col_1"]
    assert grid_sql_rows(grid, chunks[0]["metadata"]) == [
        ["Alice", "90"],
        ["Bob", "80"],
    ]


def test_xlsx_leading_title_row_and_hidden_empty_columns_keep_headers_aligned(
    worker_contract_environment: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    import openpyxl

    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
    from shared.services.retrieval.hydration.table_grid import (
        grid_sql_rows,
        html_to_grid,
        sql_columns,
    )

    del worker_contract_environment
    monkeypatch.setattr(
        _HEADER_DETECT,
        lambda self, rows_text, rowspan_hint: (
            2,
            1,
            _last_row_from_rows_text(rows_text),
        ),
    )
    workbook_path = tmp_path / "boq.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "清单"
    sheet.merge_cells("A1:E1")
    sheet["A1"] = "分部分项工程量清单"
    # Row 2 is blank; real 2-row header starts on Row 3.
    sheet.merge_cells("A3:A4")
    sheet["A3"] = "序号"
    sheet["B3"] = "金额（元）"
    sheet["B4"] = "综合单价"
    sheet.column_dimensions["C"].hidden = True
    sheet.column_dimensions["D"].hidden = True
    sheet["E4"] = "合价"
    sheet["A5"] = 1
    sheet["B5"] = 2.5
    sheet["E5"] = 100.0
    workbook.save(workbook_path)

    parse_output = _parse_xlsx(
        workbook_path, tmp_path / "parser-output", "boq.xlsx"
    )
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    assert len(parsed_df) == 1
    metadata = _extra_metadata(parsed_df)
    assert metadata["caption"] == "分部分项工程量清单"
    assert metadata["header_row_count"] == 2
    assert metadata["row_header_col_count"] == 1
    assert metadata["column_headers"] == ["序号", "金额（元）-->综合单价", "合价"]
    assert metadata["excel_range"] == "A3:E5"

    table_html = (
        Path(parse_output.output_dir) / parsed_df["content"].iloc[0]
    ).read_text(encoding="utf-8")
    chunks = dataframe_to_chunks(parsed_df)
    grid = html_to_grid(table_html)
    assert sql_columns(grid, chunks[0]["metadata"]) == [
        "row_header",
        "金额_元____综合单价",
        "合价",
    ]
    assert grid_sql_rows(grid, chunks[0]["metadata"]) == [["1", "2.5", "100"]]

