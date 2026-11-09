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
from openpyxl.styles import Border, Side  # noqa: E402
from openpyxl.workbook.defined_name import DefinedName  # noqa: E402
from openpyxl.worksheet.table import Table, TableColumn  # noqa: E402

from app.services.document_parser.formats.excel.region_split import (  # noqa: E402
    split_sheet_regions,
)
from app.services.document_parser.formats.excel.workbook_grid import (  # noqa: E402
    Region,
    load_workbook_grid,
)

_THIN = Side(style="thin")
_BOX = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _save(path: Path, workbook: Workbook) -> Path:
    workbook.save(path)
    workbook.close()
    return path


def test_empty_row_keeps_two_geometric_regions(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
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
    path = _save(tmp_path / "two.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    regions = split_sheet_regions(grid.sheets[0], defined_names=grid.defined_names)
    assert regions == [
        Region(min_row=1, min_col=1, max_row=3, max_col=2),
        Region(min_row=5, min_col=1, max_row=7, max_col=2),
    ]


def test_excel_table_seed_keeps_internal_blank_row(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["C1"] = "Extra"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["C2"] = 1
    sheet["A3"] = "East"
    sheet["B3"] = 11
    sheet["C3"] = 2
    sheet["A5"] = "South"
    sheet["B5"] = 20
    sheet["C5"] = 3
    sheet["A6"] = "West"
    sheet["B6"] = 21
    sheet["C6"] = 4
    table = Table(displayName="Sales", ref="A1:C6")
    table.tableColumns = [
        TableColumn(id=1, name="Name"),
        TableColumn(id=2, name="Amount"),
        TableColumn(id=3, name="Extra"),
    ]
    sheet.add_table(table)
    path = _save(tmp_path / "seed.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    regions = split_sheet_regions(grid.sheets[0], defined_names=grid.defined_names)
    assert regions == [Region(min_row=1, min_col=1, max_row=6, max_col=3)]


def test_rectangular_defined_name_is_a_seed(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["A6"] = "Name"
    sheet["B6"] = "Amount"
    sheet["A7"] = "South"
    sheet["B7"] = 20
    workbook.defined_names.add(DefinedName(name="BlockOne", attr_text="Data!$A$1:$B$2"))
    path = _save(tmp_path / "named.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    regions = split_sheet_regions(grid.sheets[0], defined_names=grid.defined_names)
    assert Region(min_row=1, min_col=1, max_row=2, max_col=2) in regions
    assert Region(min_row=6, min_col=1, max_row=7, max_col=2) in regions


def test_formula_fragment_is_not_absorbed(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["A3"] = "East"
    sheet["B3"] = 11
    sheet["B5"] = "=SUM(B2:B3)"
    path = _save(tmp_path / "sat.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    regions = split_sheet_regions(grid.sheets[0], defined_names=grid.defined_names)
    assert Region(min_row=1, min_col=1, max_row=3, max_col=2) in regions
    assert any(region.min_row == 5 and region.max_row == 5 for region in regions)


def test_border_block_rejoins_empty_row_split(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    cells = {
        "A1": "Name",
        "B1": "Amount",
        "A2": "North",
        "B2": 10,
        "A3": "East",
        "B3": 11,
        "A5": "Name",
        "B5": "Amount",
        "A6": "South",
        "B6": 20,
        "A7": "West",
        "B7": 21,
    }
    for address, value in cells.items():
        sheet[address] = value
        sheet[address].border = _BOX
    sheet["A4"].border = _BOX
    sheet["B4"].border = _BOX
    path = _save(tmp_path / "border.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    regions = split_sheet_regions(grid.sheets[0], defined_names=grid.defined_names)
    assert regions == [Region(min_row=1, min_col=1, max_row=7, max_col=2)]


def test_caption_row_attaches_to_table_below(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.merge_cells("A1:B1")
    sheet["A1"] = "Monthly Report"
    sheet["A3"] = "Name"
    sheet["B3"] = "Amount"
    sheet["A4"] = "North"
    sheet["B4"] = 10
    path = _save(tmp_path / "caption.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    regions = split_sheet_regions(grid.sheets[0], defined_names=grid.defined_names)
    assert len(regions) == 1
    assert regions[0].min_row == 1
    assert regions[0].max_row == 4
