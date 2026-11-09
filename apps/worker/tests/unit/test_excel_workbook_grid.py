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
    display_value,
    expand_visual_grid,
    load_workbook_grid,
    sheet_display_value,
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
    assert display_value(formula_cell) == "=A1*2"
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
    assert sheet_display_value(sheet_grid, 1, 2) == "Title"


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
