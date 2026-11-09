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

from app.services.document_parser.formats.excel.luna_decisions import (  # noqa: E402
    split_sheet_regions,
)
from app.services.document_parser.formats.excel.workbook_grid import (  # noqa: E402
    Region,
    load_workbook_grid,
)


_THIN = Side(style="thin")
_BOX = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


class _FailClosedJudge:
    def choose(self, **kwargs):
        return None


class _MergeJudge:
    def choose(self, *, required_ids, **kwargs):
        return {question_id: "merge" for question_id in required_ids}


def _save(path: Path, workbook: Workbook) -> Path:
    workbook.save(path)
    workbook.close()
    return path


def _regions(classified):
    return [item.region for item in classified]


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
    classified = split_sheet_regions(
        grid.sheets[0],
        defined_names=grid.defined_names,
        judge=_FailClosedJudge(),
    )
    assert _regions(classified) == [
        Region(min_row=1, min_col=1, max_row=3, max_col=2),
        Region(min_row=5, min_col=1, max_row=7, max_col=2),
    ]
    assert [item.role for item in classified] == ["table", "table"]


def test_luna_merge_rejoins_blocks_across_empty_row(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Name"
    sheet["B1"] = "Amount"
    sheet["A2"] = "North"
    sheet["B2"] = 10
    sheet["A4"] = "East"
    sheet["B4"] = 11
    path = _save(tmp_path / "merge.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    classified = split_sheet_regions(
        grid.sheets[0],
        defined_names=grid.defined_names,
        judge=_MergeJudge(),
    )
    assert _regions(classified) == [
        Region(min_row=1, min_col=1, max_row=4, max_col=2)
    ]
    assert classified[0].role == "table"


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
    classified = split_sheet_regions(
        grid.sheets[0],
        defined_names=grid.defined_names,
        judge=_FailClosedJudge(),
    )
    assert _regions(classified) == [Region(min_row=1, min_col=1, max_row=6, max_col=3)]
    assert classified[0].locked is True
    assert classified[0].role == "table"


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
    classified = split_sheet_regions(
        grid.sheets[0],
        defined_names=grid.defined_names,
        judge=_FailClosedJudge(),
    )
    regions = _regions(classified)
    assert Region(min_row=1, min_col=1, max_row=2, max_col=2) in regions
    assert Region(min_row=6, min_col=1, max_row=7, max_col=2) in regions
    locked = [item for item in classified if item.locked]
    assert len(locked) == 1
    assert locked[0].role == "table"


def test_formula_fragment_is_not_absorbed_by_split(tmp_path: Path) -> None:
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
    classified = split_sheet_regions(
        grid.sheets[0],
        defined_names=grid.defined_names,
        judge=_FailClosedJudge(),
    )
    regions = _regions(classified)
    assert Region(min_row=1, min_col=1, max_row=3, max_col=2) in regions
    fragment = next(item for item in classified if item.region.min_row == 5)
    assert fragment.role == "fragment"
    assert fragment.region.max_row == 5


def test_border_block_does_not_rejoin_empty_row_split(tmp_path: Path) -> None:
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
    classified = split_sheet_regions(
        grid.sheets[0],
        defined_names=grid.defined_names,
        judge=_FailClosedJudge(),
    )
    assert _regions(classified) == [
        Region(min_row=1, min_col=1, max_row=3, max_col=2),
        Region(min_row=5, min_col=1, max_row=7, max_col=2),
    ]


def test_one_column_sparse_numbers_stay_one_region(tmp_path: Path) -> None:
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
    sheet["A4"] = "West"
    sheet["B4"] = 12
    sheet["C4"] = 3
    sheet["E1"] = 1
    sheet["E3"] = 1
    sheet["E5"] = 1
    path = _save(tmp_path / "sparse-col.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    classified = split_sheet_regions(
        grid.sheets[0],
        defined_names=grid.defined_names,
        judge=_FailClosedJudge(),
    )
    assert _regions(classified) == [
        Region(min_row=1, min_col=1, max_row=4, max_col=3),
        Region(min_row=1, min_col=5, max_row=5, max_col=5),
    ]
    assert [item.role for item in classified] == ["table", "fragment"]


def test_caption_across_blank_row_stays_a_fragment(tmp_path: Path) -> None:
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
    classified = split_sheet_regions(
        grid.sheets[0],
        defined_names=grid.defined_names,
        judge=_FailClosedJudge(),
    )
    assert len(classified) == 2
    assert classified[0].role == "fragment"
    assert classified[0].region == Region(min_row=1, min_col=1, max_row=1, max_col=2)
    assert classified[1].role == "table"
    assert classified[1].region.min_row == 3


def test_adjacent_caption_merges_onto_seed_table_in_fail_closed_fallback(
    tmp_path: Path,
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.merge_cells("A1:B1")
    sheet["A1"] = "Monthly Report"
    sheet["A2"] = "Name"
    sheet["B2"] = "Amount"
    sheet["A3"] = "North"
    sheet["B3"] = 10
    table = Table(displayName="Sales", ref="A2:B3")
    table.tableColumns = [
        TableColumn(id=1, name="Name"),
        TableColumn(id=2, name="Amount"),
    ]
    sheet.add_table(table)
    path = _save(tmp_path / "adjacent-caption.xlsx", workbook)

    grid = load_workbook_grid(str(path))
    classified = split_sheet_regions(
        grid.sheets[0],
        defined_names=grid.defined_names,
        judge=_FailClosedJudge(),
    )
    assert _regions(classified) == [
        Region(min_row=1, min_col=1, max_row=3, max_col=2)
    ]
    assert classified[0].role == "table"
