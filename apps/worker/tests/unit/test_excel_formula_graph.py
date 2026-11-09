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
from openpyxl.worksheet.table import Table, TableColumn  # noqa: E402

from app.services.document_parser.formats.excel.formula_graph import (  # noqa: E402
    CellRange,
    build_formula_graph,
    parse_formula_refs,
)
from app.services.document_parser.formats.excel.workbook_grid import (  # noqa: E402
    load_workbook_grid,
)


def _range(
    sheet: str, min_row: int, min_col: int, max_row: int, max_col: int
) -> CellRange:
    return CellRange(
        sheet=sheet,
        min_row=min_row,
        min_col=min_col,
        max_row=max_row,
        max_col=max_col,
    )


def test_parse_formula_refs_covers_a1_forms() -> None:
    bounds = {"Data": (10, 5), "Sheet": (8, 3), "Sheet Name": (20, 4)}
    assert parse_formula_refs("=A1", "Data", [], sheet_bounds=bounds) == (
        _range("Data", 1, 1, 1, 1),
    )
    assert parse_formula_refs("=$A$1", "Data", [], sheet_bounds=bounds) == (
        _range("Data", 1, 1, 1, 1),
    )
    assert parse_formula_refs("=A1:B10", "Data", [], sheet_bounds=bounds) == (
        _range("Data", 1, 1, 10, 2),
    )
    assert parse_formula_refs("=Sheet!A1", "Data", [], sheet_bounds=bounds) == (
        _range("Sheet", 1, 1, 1, 1),
    )
    assert parse_formula_refs("='Sheet Name'!B:B", "Data", [], sheet_bounds=bounds) == (
        _range("Sheet Name", 1, 2, 20, 2),
    )
    assert parse_formula_refs("=A:A", "Data", [], sheet_bounds=bounds) == (
        _range("Data", 1, 1, 10, 1),
    )
    assert parse_formula_refs("=3:3", "Data", [], sheet_bounds=bounds) == (
        _range("Data", 3, 1, 3, 5),
    )


def test_parse_formula_refs_expands_defined_name_and_table(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Amount"
    sheet["A2"] = 1
    sheet["A3"] = 2
    sheet["B2"] = "=SUM(Table1[Amount])"
    sheet["C2"] = "=TotalRange"
    table = Table(displayName="Table1", ref="A1:A3")
    table.tableColumns = [TableColumn(id=1, name="Amount")]
    sheet.add_table(table)
    workbook.defined_names.add(DefinedName(name="TotalRange", attr_text="Data!$A$2:$A$3"))
    path = tmp_path / "refs.xlsx"
    workbook.save(path)
    workbook.close()

    nodes = {
        (node.row, node.col): node
        for node in build_formula_graph(load_workbook_grid(str(path)))
    }
    table_node = nodes[(2, 2)]
    name_node = nodes[(2, 3)]
    amount_column = _range("Data", 2, 1, 3, 1)
    assert table_node.formula == "=SUM(Table1[Amount])"
    assert table_node.functions == ("SUM",)
    assert table_node.unresolved is False
    assert table_node.refs == (amount_column,)
    assert name_node.formula == "=TotalRange"
    assert name_node.unresolved is False
    assert name_node.refs == (amount_column,)


def test_indirect_and_offset_formulas_are_unresolved(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = 1
    sheet["B1"] = "=INDIRECT(A1)"
    sheet["C1"] = "=OFFSET(A1,1,1)"
    path = tmp_path / "dynamic.xlsx"
    workbook.save(path)
    workbook.close()

    nodes = {
        (node.row, node.col): node
        for node in build_formula_graph(load_workbook_grid(str(path)))
    }
    indirect = nodes[(1, 2)]
    offset = nodes[(1, 3)]
    assert indirect.unresolved is True
    assert offset.unresolved is True
    assert indirect.functions == ("INDIRECT",)
    assert offset.functions == ("OFFSET",)
    assert _range("Data", 1, 1, 1, 1) in indirect.refs
    assert _range("Data", 1, 1, 1, 1) in offset.refs
