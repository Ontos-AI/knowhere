from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from openpyxl import Workbook  # noqa: E402
from openpyxl.workbook.defined_name import DefinedName  # noqa: E402
from openpyxl.worksheet.formula import ArrayFormula  # noqa: E402
from openpyxl.worksheet.table import Table, TableColumn  # noqa: E402

from app.services.document_parser.formats.excel.workbook_grid import (  # noqa: E402
    Region,
    cell_has_value_or_formula,
    cell_text,
    expand_visual_grid,
    load_workbook_grid,
)


def _save(path: Path, workbook: Workbook) -> Path:
    workbook.save(path)
    workbook.close()
    return path


def test_load_workbook_grid_keeps_cached_value_and_formula(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = 10
    sheet["B1"] = "=A1*2"
    path = _save(tmp_path / "grid.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    data = grid.sheet_by_name()["Data"]
    cached = data.cells[(1, 1)]
    formula_cell = data.cells[(1, 2)]
    assert cached.value == 10
    assert cached.formula is None
    assert formula_cell.formula == "=A1*2"
    assert formula_cell.value is None
    assert cell_text(formula_cell) == "=A1*2"
    assert cell_has_value_or_formula(data, 1, 2) is True
    assert cell_has_value_or_formula(data, 2, 1) is False


def test_expand_visual_grid_copies_merge_origin(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.merge_cells("A1:B1")
    sheet["A1"] = "Title"
    path = _save(tmp_path / "merge.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    sheet_grid = grid.sheets[0]
    visual = expand_visual_grid(
        sheet_grid, Region(min_row=1, min_col=1, max_row=1, max_col=2)
    )
    assert visual[0][0] is not None
    assert visual[0][0].value == "Title"
    assert visual[0][0].colspan == 2
    assert visual[0][1] is not None
    assert visual[0][1].merge_origin == (1, 1)


def test_load_workbook_grid_reads_array_formula_and_table(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Amount"
    sheet["A2"] = 1
    sheet["A3"] = 2
    sheet["B1"] = ArrayFormula(ref="B1", text="=A2:A2")
    table = Table(displayName="Table1", ref="A1:A3")
    table.tableColumns = [TableColumn(id=1, name="Amount")]
    sheet.add_table(table)
    workbook.defined_names.add(DefinedName(name="TotalRange", attr_text="Data!$A$2:$A$3"))
    path = _save(tmp_path / "table.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    data = grid.sheet_by_name()["Data"]
    assert data.cells[(1, 2)].formula == "=A2:A2"
    assert data.excel_tables[0].name == "Table1"
    assert data.excel_tables[0].columns == ("Amount",)
    assert grid.defined_names[0].name == "TotalRange"
    assert grid.defined_names[0].attr_text == "Data!$A$2:$A$3"


def test_cell_text_follows_date_percent_decimal_unit_and_boolean_formats(tmp_path: Path) -> None:
    import datetime

    workbook = Workbook()
    sheet = workbook.active
    cases = [
        (datetime.datetime(2024, 1, 1), "yyyy-mm-dd", "2024-01-01"),
        (datetime.datetime(2026, 9, 12), "YYYY-MM-DD", "2026-09-12"),
        (datetime.datetime(2026, 3, 3), "[$-804]yyyy\\年m\\月d\\日", "2026-03-03"),
        (datetime.datetime(2024, 1, 1, 9, 30), "yyyy-mm-dd hh:mm", "2024-01-01 09:30"),
        (datetime.datetime(2024, 1, 1, 9, 30, 15), "m/d/yy h:mm:ss", "2024-01-01 09:30:15"),
        (datetime.time(9, 30), "hh:mm", "09:30"),
        (datetime.timedelta(hours=27, minutes=5), "[h]:mm", "27:05"),
        (0.15, "0%", "15%"),
        (0.1234, "0.00%", "12.34%"),
        (1234567.891, "#,##0.00", "1234567.89"),
        (1234567.891, "#,##0", "1234568"),
        (2.5, "0.0#", "2.5"),
        (9.225, "0.00", "9.23"),
        (2.5, "0", "3"),
        (0.0145, "0.00%", "1.45%"),
        (-0.125, "0.00", "-0.13"),
        (2.567, "0.0#", "2.57"),
        (-3.0, '[Red]0.00;[Blue]-0.00', "-3.00"),
        (12.0, "General", "12"),
        (0.25, "General", "0.25"),
        (1e-07, "0.00E+00", "1e-07"),
        (True, "General", "TRUE"),
        ("001", "@", "001"),
        (3.254, '0.00"x"', "3.25x"),
        (1234.5, '#,##0.00"万元"', "1234.50万元"),
        (12, '0 "kg"', "12 kg"),
        (5, '0\\件', "5件"),
        (8, 'General"人"', "8人"),
        (0.05, '0%"增长"', "5%增长"),
        (1234.5, '"¥"#,##0.00', "1234.50"),
        (1234, "$#,##0;[Red]($#,##0);-", "1234"),
        (1234.5, '_(* #,##0.00_);_(* (#,##0.00);_(* "-"??_);_(@_)', "1234.50"),
        (12345678, '0.0,,"M"', "12.3M"),
        (12345, "#,##0,", "12"),
    ]
    for column, (value, number_format, _expected) in enumerate(cases, start=1):
        cell = sheet.cell(row=1, column=column, value=value)
        cell.number_format = number_format
    path = _save(tmp_path / "formats.xlsx", workbook)

    data = load_workbook_grid(str(path)).sheets[0]

    assert [
        cell_text(data.cells[(1, column)]) for column in range(1, len(cases) + 1)
    ] == [expected for _value, _format, expected in cases]
