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
_HEADER_LOCATE = (
    "app.services.document_parser.formats.excel.header_detect."
    "TextClientSpreadsheetHeaderDetector.locate_header_start"
)
_LUNA_CHOOSE = (
    "app.services.document_parser.formats.excel.luna_decisions."
    "GatewaySpreadsheetRegionJudge.choose"
)


@pytest.fixture(autouse=True)
def _fail_closed_spreadsheet_headers(
    monkeypatch: MonkeyPatch,
    worker_contract_environment: None,
) -> None:
    del worker_contract_environment
    monkeypatch.setattr(
        _HEADER_DETECT,
        lambda self, rows_text: None,
    )
    monkeypatch.setattr(_HEADER_LOCATE, lambda self, rows_text: None)
    monkeypatch.setattr(_LUNA_CHOOSE, lambda self, **kwargs: None)


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
        lambda self, rows_text: (2, 1),
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
    assert 'colspan="2"' in table_html
    assert "<tfoot" in table_html

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
        lambda self, rows_text: (1, 1),
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
        lambda self, rows_text: (0, 0),
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
        lambda self, rows_text: (2, 1),
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
    assert parsed_df["path"].tolist() == ["boq.xlsx/清单"]
    metadata = _extra_metadata(parsed_df)
    assert metadata["caption"] == "A1=分部分项工程量清单"
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


def test_xlsx_rows_above_header_start_become_caption(
    worker_contract_environment: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    import openpyxl

    from app.services.document_parser.formats.excel.header_detect import HeaderStart
    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
    from shared.services.retrieval.hydration.table_grid import (
        grid_sql_rows,
        html_to_grid,
        sql_columns,
    )

    del worker_contract_environment
    monkeypatch.setattr(_HEADER_LOCATE, lambda self, rows_text: HeaderStart(row=2))
    monkeypatch.setattr(_HEADER_DETECT, lambda self, rows_text: (2, 1))
    workbook_path = tmp_path / "piles.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "桩基"
    sheet.merge_cells("A1:C1")
    sheet["A1"] = "人工挖孔桩基础收方单"
    sheet["A2"] = "桩身保护层"
    sheet["B2"] = 0.05
    sheet.merge_cells("A3:A4")
    sheet["A3"] = "序号"
    sheet.merge_cells("B3:C3")
    sheet["B3"] = "设计尺寸"
    sheet["B4"] = "桩径"
    sheet["C4"] = "桩半径"
    sheet["A5"] = 1
    sheet["B5"] = 2.1
    sheet["C5"] = 1.05
    workbook.save(workbook_path)

    parse_output = _parse_xlsx(
        workbook_path, tmp_path / "parser-output", "piles.xlsx"
    )
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    assert len(parsed_df) == 1
    metadata = _extra_metadata(parsed_df)
    assert metadata["excel_range"] == "A1:C5"
    assert metadata["header_row_count"] == 2
    assert metadata["column_headers"] == ["序号", "设计尺寸-->桩径", "设计尺寸-->桩半径"]
    assert "人工挖孔桩基础收方单" in metadata["caption"]
    assert "B2=0.05" in metadata["caption"]
    table_html = (
        Path(parse_output.output_dir) / parsed_df["content"].iloc[0]
    ).read_text(encoding="utf-8")
    chunks = dataframe_to_chunks(parsed_df)
    grid = html_to_grid(table_html)
    assert sql_columns(grid, chunks[0]["metadata"]) == [
        "row_header",
        "设计尺寸___桩径",
        "设计尺寸___桩半径",
    ]
    assert grid_sql_rows(grid, chunks[0]["metadata"]) == [["1", "2.1", "1.05"]]


def test_xlsx_uncached_formula_cells_do_not_split_table(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    import openpyxl

    del worker_contract_environment
    workbook_path = tmp_path / "formulas.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Totals"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["A3"] = "East"
    sheet["B3"] = 11
    sheet["B4"] = "=B2"
    sheet["A5"] = "South"
    sheet["B5"] = 20
    sheet["A6"] = "West"
    sheet["B6"] = 21
    workbook.save(workbook_path)

    parse_output = _parse_xlsx(
        workbook_path, tmp_path / "parser-output", "formulas.xlsx"
    )
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    assert len(parsed_df) == 1
    metadata = _extra_metadata(parsed_df)
    assert metadata["excel_range"] == "A1:B6"
    assert parsed_df["path"].tolist() == ["formulas.xlsx/Totals"]

    table_html = (
        Path(parse_output.output_dir) / parsed_df["content"].iloc[0]
    ).read_text(encoding="utf-8")
    assert "North" in table_html
    assert "South" in table_html
    assert "=B2" in table_html
    assert "data-formula" in table_html
    assert "<tfoot" in table_html


def test_xlsx_empty_row_keeps_two_tables(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    import openpyxl

    del worker_contract_environment
    workbook_path = tmp_path / "two-tables.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Split"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["A3"] = "East"
    sheet["B3"] = 11
    sheet["A5"] = "Name"
    sheet["B5"] = "Amount"
    sheet["A6"] = "South"
    sheet["B6"] = 20
    sheet["A7"] = "West"
    sheet["B7"] = 21
    workbook.save(workbook_path)

    parse_output = _parse_xlsx(
        workbook_path, tmp_path / "parser-output", "two-tables.xlsx"
    )
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    assert len(parsed_df) == 2
    html_text = "\n".join(
        (Path(parse_output.output_dir) / path).read_text(encoding="utf-8")
        for path in parsed_df["content"].tolist()
    )
    assert "North" in html_text
    assert "South" in html_text
    ranges = {_extra_metadata(parsed_df.iloc[[index]])["excel_range"] for index in range(2)}
    assert ranges == {"A1:B3", "A5:B7"}


def test_xlsx_html_preserves_merges_formula_and_tfoot(
    worker_contract_environment: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    import openpyxl

    del worker_contract_environment
    monkeypatch.setattr(
        _HEADER_DETECT,
        lambda self, rows_text: (1, 1),
    )
    workbook_path = tmp_path / "html-contract.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "P&L"
    sheet.merge_cells("B1:C1")
    sheet["A1"] = "Item"
    sheet["B1"] = 2024
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["C2"] = "=B2"
    workbook.save(workbook_path)

    parse_output = _parse_xlsx(
        workbook_path, tmp_path / "parser-output", "html-contract.xlsx"
    )
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    table_html = (
        Path(parse_output.output_dir) / parsed_df["content"].iloc[0]
    ).read_text(encoding="utf-8")
    assert 'colspan="2"' in table_html
    assert "data-formula" in table_html
    assert "=B2" in table_html
    assert "<tfoot" in table_html
    assert 'scope="row"' in table_html
    metadata = _extra_metadata(parsed_df)
    assert metadata["excel_range"] == "A1:C2"
    assert metadata["sheet_name"] == "P&L"


def test_xlsx_sum_fragment_lands_in_tfoot_and_emits_one_chunk(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    import openpyxl

    del worker_contract_environment
    workbook_path = tmp_path / "sum-tfoot.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["A3"] = "East"
    sheet["B3"] = 11
    sheet["A5"] = "合计"
    sheet["B5"] = "=SUM(B2:B3)"
    workbook.save(workbook_path)

    parse_output = _parse_xlsx(
        workbook_path, tmp_path / "parser-output", "sum-tfoot.xlsx"
    )
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    assert len(parsed_df) == 1
    metadata = _extra_metadata(parsed_df)
    assert metadata["excel_range"] == "A1:B3"
    table_html = (
        Path(parse_output.output_dir) / parsed_df["content"].iloc[0]
    ).read_text(encoding="utf-8")
    assert "<tfoot>" in table_html
    assert 'data-formula="=SUM(B2:B3)"' in table_html
    assert 'data-excel-r="5"' in table_html
    assert "合计" in table_html
    assert "<tbody>" in table_html
    assert "North" in table_html


def test_xlsx_cross_sheet_kpi_is_absorbed_into_data_table(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    import openpyxl

    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
    from shared.services.retrieval.search.lexical_text import build_content_lexical_text

    del worker_contract_environment
    workbook_path = tmp_path / "kpi.xlsx"
    workbook = openpyxl.Workbook()
    data = workbook.active
    data.title = "Data"
    data["A1"] = "Name"
    data["B1"] = "Amount"
    data["A2"] = "North"
    data["B2"] = 10
    data["A3"] = "East"
    data["B3"] = 20
    summary = workbook.create_sheet("Summary")
    summary["A1"] = "Gross Margin"
    summary["B1"] = "=(Data!B2-Data!B3)/Data!B2"
    workbook.save(workbook_path)

    parse_output = _parse_xlsx(workbook_path, tmp_path / "parser-output", "kpi.xlsx")
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    assert len(parsed_df) == 1
    metadata = _extra_metadata(parsed_df)
    assert metadata["sheet_name"] == "Data"
    assert metadata["excel_range"] == "A1:B3"
    assert "satellites" in metadata
    assert any("Gross Margin" in str(item) for item in metadata["satellites"])
    table_html = (
        Path(parse_output.output_dir) / parsed_df["content"].iloc[0]
    ).read_text(encoding="utf-8")
    assert "Gross Margin" in table_html
    chunks = dataframe_to_chunks(parsed_df)
    lexical = build_content_lexical_text(chunks[0]) or ""
    assert "Gross Margin" in lexical


def test_xlsx_formula_fragment_belongs_to_both_host_tables(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    import openpyxl

    del worker_contract_environment
    workbook_path = tmp_path / "dual.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["D1"] = "Name"
    sheet["E1"] = "Amount"
    sheet["D2"] = "South"
    sheet["E2"] = 20
    sheet["C4"] = "=B2+E2"
    workbook.save(workbook_path)

    parse_output = _parse_xlsx(workbook_path, tmp_path / "parser-output", "dual.xlsx")
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    assert len(parsed_df) == 2
    satellites = [
        _extra_metadata(parsed_df.iloc[[index]]).get("satellites") or []
        for index in range(2)
    ]
    assert all(satellites)
    html_text = "\n".join(
        (Path(parse_output.output_dir) / path).read_text(encoding="utf-8")
        for path in parsed_df["content"].tolist()
    )
    assert html_text.count("B2+E2") == 2


def test_xlsx_vlookup_keeps_two_tables_and_derives_edges(
    worker_contract_environment: None,
    tmp_path: Path,
) -> None:
    import openpyxl

    from shared.services.chunks.dataframe_chunk_converter import dataframe_to_chunks
    from shared.services.chunks.chunk_connections import (
        RELATION_DERIVED_FROM,
        RELATION_DERIVES,
    )

    del worker_contract_environment
    workbook_path = tmp_path / "vlookup.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Rate"
    sheet["A2"] = "USD"
    sheet["B2"] = 7
    sheet["D1"] = "Name"
    sheet["E1"] = "Amount"
    sheet["D2"] = "USD"
    sheet["E2"] = "=VLOOKUP(D2,A1:B2,2,FALSE)"
    workbook.save(workbook_path)

    parse_output = _parse_xlsx(
        workbook_path, tmp_path / "parser-output", "vlookup.xlsx"
    )
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    assert len(parsed_df) == 2
    chunks = dataframe_to_chunks(parsed_df)
    relations = {
        item["relation"]
        for chunk in chunks
        for item in chunk["metadata"].get("connect_to") or []
    }
    assert RELATION_DERIVES in relations
    assert RELATION_DERIVED_FROM in relations
    refs = [
        item.get("ref")
        for chunk in chunks
        for item in chunk["metadata"].get("connect_to") or []
    ]
    assert any(ref and "VLOOKUP" in str(ref) for ref in refs)


def test_xlsx_luna_merge_rejoins_same_table_across_empty_row(
    worker_contract_environment: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    import openpyxl

    del worker_contract_environment

    def _merge_choose(self, *, required_ids, **kwargs):
        return {question_id: "merge" for question_id in required_ids}

    monkeypatch.setattr(_LUNA_CHOOSE, _merge_choose)
    workbook_path = tmp_path / "luna-merge.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["A4"] = "East"
    sheet["B4"] = 11
    workbook.save(workbook_path)

    parse_output = _parse_xlsx(
        workbook_path, tmp_path / "parser-output", "luna-merge.xlsx"
    )
    parsed_df = parse_output.parsed_df
    assert parsed_df is not None
    assert len(parsed_df) == 1
    metadata = _extra_metadata(parsed_df)
    assert metadata["excel_range"] == "A1:B4"
    table_html = (
        Path(parse_output.output_dir) / parsed_df["content"].iloc[0]
    ).read_text(encoding="utf-8")
    assert "North" in table_html
    assert "East" in table_html

