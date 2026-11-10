from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import openpyxl
from openpyxl.utils import column_index_from_string
from openpyxl.utils.cell import range_boundaries
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

from shared.core.exceptions.domain_exceptions import TableParsingException


@dataclass(frozen=True)
class Region:
    min_row: int
    min_col: int
    max_row: int
    max_col: int


@dataclass(frozen=True)
class GridCell:
    row: int
    col: int
    value: object
    formula: str | None
    merge_origin: tuple[int, int] | None
    rowspan: int
    colspan: int


@dataclass(frozen=True)
class ExcelTableDef:
    name: str
    sheet: str
    min_row: int
    min_col: int
    max_row: int
    max_col: int
    header_row_count: int
    totals_row_count: int
    columns: tuple[str, ...]


@dataclass(frozen=True)
class DefinedNameDef:
    name: str
    sheet: str | None
    attr_text: str
    unresolved: bool = False


@dataclass(frozen=True)
class SheetGrid:
    name: str
    hidden: bool
    max_row: int
    max_col: int
    cells: dict[tuple[int, int], GridCell]
    merged_ranges: list[tuple[int, int, int, int]]
    excel_tables: list[ExcelTableDef]
    hidden_columns: frozenset[int]


@dataclass(frozen=True)
class WorkbookGrid:
    sheets: list[SheetGrid]
    defined_names: list[DefinedNameDef]

    def sheet_by_name(self) -> dict[str, SheetGrid]:
        return {sheet.name: sheet for sheet in self.sheets}

    def sheet_bounds(self) -> dict[str, tuple[int, int]]:
        return {sheet.name: (sheet.max_row, sheet.max_col) for sheet in self.sheets}


def load_workbook_grid(xlsx_path: str) -> WorkbookGrid:
    """Load cached values and formulas by opening the workbook twice."""
    workbook_values = None
    workbook_formulas = None
    try:
        workbook_values = openpyxl.load_workbook(xlsx_path, data_only=True)
        workbook_formulas = openpyxl.load_workbook(xlsx_path, data_only=False)
        return _workbook_from_pair(workbook_values, workbook_formulas)
    except Exception as exc:
        raise TableParsingException(
            user_message="Failed to load spreadsheet grid",
            reason="EXCEL_WORKBOOK_GRID_LOAD_FAILED",
            internal_message=str(exc),
            original_exception=exc,
        ) from exc
    finally:
        if workbook_values is not None:
            workbook_values.close()
        if workbook_formulas is not None:
            workbook_formulas.close()


def expand_visual_grid(
    sheet: SheetGrid, region: Region
) -> list[list[GridCell | None]]:
    if region.max_row < region.min_row or region.max_col < region.min_col:
        return []
    rows = region.max_row - region.min_row + 1
    cols = region.max_col - region.min_col + 1
    grid: list[list[GridCell | None]] = [
        [None] * cols for _ in range(rows)
    ]
    for row in range(region.min_row, region.max_row + 1):
        for col in range(region.min_col, region.max_col + 1):
            rel_row = row - region.min_row
            rel_col = col - region.min_col
            merge = merge_at(sheet, row, col)
            if merge is None:
                grid[rel_row][rel_col] = sheet.cells.get((row, col))
                continue
            origin_row, origin_col, max_row, max_col = merge
            origin = sheet.cells.get((origin_row, origin_col))
            rowspan = max_row - origin_row + 1
            colspan = max_col - origin_col + 1
            merge_origin = (
                None
                if (row, col) == (origin_row, origin_col)
                else (origin_row, origin_col)
            )
            if origin is None:
                grid[rel_row][rel_col] = GridCell(
                    row=row,
                    col=col,
                    value=None,
                    formula=None,
                    merge_origin=merge_origin,
                    rowspan=rowspan,
                    colspan=colspan,
                )
                continue
            grid[rel_row][rel_col] = GridCell(
                row=row,
                col=col,
                value=origin.value,
                formula=origin.formula,
                merge_origin=merge_origin,
                rowspan=rowspan,
                colspan=colspan,
            )
    return grid


def display_value(cell: GridCell | None) -> object:
    if cell is None:
        return None
    if cell.value is not None:
        return cell.value
    if cell.formula:
        return cell.formula
    return None


def cell_has_value_or_formula(sheet: SheetGrid, row: int, col: int) -> bool:
    cell = sheet.cells.get((row, col))
    return cell is not None and (cell.value is not None or cell.formula is not None)


def merge_at(
    sheet: SheetGrid, row: int, col: int
) -> tuple[int, int, int, int] | None:
    for merged in sheet.merged_ranges:
        min_row, min_col, max_row, max_col = merged
        if min_row <= row <= max_row and min_col <= col <= max_col:
            return merged
    return None


def _workbook_from_pair(workbook_values, workbook_formulas) -> WorkbookGrid:
    value_sheets: Mapping = {
        name: workbook_values[name]
        for name in workbook_values.sheetnames
        if name in workbook_values
    }
    sheets: list[SheetGrid] = []
    for sheet_name in workbook_formulas.sheetnames:
        formula_sheet = workbook_formulas[sheet_name]
        value_sheet = value_sheets.get(sheet_name)
        sheets.append(
            _sheet_from_pair(
                name=sheet_name,
                formula_sheet=formula_sheet,
                value_sheet=value_sheet,
            )
        )
    return WorkbookGrid(
        sheets=sheets,
        defined_names=_defined_names(workbook_formulas),
    )


def _sheet_from_pair(*, name: str, formula_sheet, value_sheet) -> SheetGrid:
    merged_ranges = [
        (item.min_row, item.min_col, item.max_row, item.max_col)
        for item in formula_sheet.merged_cells.ranges
    ]
    coordinates: set[tuple[int, int]] = set()
    if value_sheet is not None:
        coordinates.update(value_sheet._cells.keys())
    coordinates.update(formula_sheet._cells.keys())
    for min_row, min_col, max_row, max_col in merged_ranges:
        coordinates.add((min_row, min_col))

    cells: dict[tuple[int, int], GridCell] = {}
    for row, col in coordinates:
        formula_cell = formula_sheet.cell(row, col)
        value_cell = (
            value_sheet.cell(row, col) if value_sheet is not None else None
        )
        rowspan = 1
        colspan = 1
        merge_origin: tuple[int, int] | None = None
        covering = _covering_merge(row, col, merged_ranges)
        if covering is not None:
            min_row, min_col, max_row, max_col = covering
            rowspan = max_row - min_row + 1
            colspan = max_col - min_col + 1
            if (row, col) != (min_row, min_col):
                merge_origin = (min_row, min_col)
        cached = None if value_cell is None else value_cell.value
        formula = _formula_string(formula_cell)
        cell = GridCell(
            row=row,
            col=col,
            value=cached,
            formula=formula,
            merge_origin=merge_origin,
            rowspan=rowspan,
            colspan=colspan,
        )
        origin_cell = covering is None or (row, col) == (covering[0], covering[1])
        if _keep_cell(cell, origin_cell=origin_cell):
            cells[(row, col)] = cell

    max_row = formula_sheet.max_row or 0
    max_col = formula_sheet.max_column or 0
    if value_sheet is not None:
        max_row = max(max_row, value_sheet.max_row or 0)
        max_col = max(max_col, value_sheet.max_column or 0)
    if cells:
        max_row = max(max_row, max(row for row, _col in cells))
        max_col = max(max_col, max(col for _row, col in cells))

    return SheetGrid(
        name=name,
        hidden=formula_sheet.sheet_state != "visible",
        max_row=max_row,
        max_col=max_col,
        cells=cells,
        merged_ranges=merged_ranges,
        excel_tables=_excel_tables(formula_sheet, sheet_name=name),
        hidden_columns=_hidden_columns(formula_sheet),
    )


def _covering_merge(
    row: int, col: int, merged_ranges: list[tuple[int, int, int, int]]
) -> tuple[int, int, int, int] | None:
    for merged in merged_ranges:
        min_row, min_col, max_row, max_col = merged
        if min_row <= row <= max_row and min_col <= col <= max_col:
            return merged
    return None


def _keep_cell(cell: GridCell, *, origin_cell: bool) -> bool:
    if cell.value is not None or cell.formula is not None:
        return True
    return bool(origin_cell and (cell.rowspan > 1 or cell.colspan > 1))


def _formula_string(cell) -> str | None:
    value = cell.value
    if isinstance(value, ArrayFormula):
        text = str(value.text or "").strip()
        if not text:
            return None
        return text if text.startswith("=") else f"={text}"
    if isinstance(value, DataTableFormula):
        ref = str(value.ref or "").strip()
        if not ref:
            return None
        return f"={ref}"
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("="):
            return text
        if getattr(cell, "data_type", None) == "f" and text:
            return f"={text}"
    return None


def _hidden_columns(worksheet) -> frozenset[int]:
    hidden: set[int] = set()
    for dim in worksheet.column_dimensions.values():
        if not dim.hidden:
            continue
        if dim.min is not None and dim.max is not None:
            hidden.update(range(dim.min, dim.max + 1))
            continue
        index = getattr(dim, "index", None)
        if isinstance(index, str) and index:
            hidden.add(column_index_from_string(index))
        elif isinstance(index, int):
            hidden.add(index)
    return frozenset(hidden)


def _excel_tables(worksheet, *, sheet_name: str) -> list[ExcelTableDef]:
    tables: list[ExcelTableDef] = []
    for table in worksheet.tables.values():
        ref = str(table.ref or "").strip()
        if not ref:
            continue
        min_col, min_row, max_col, max_row = range_boundaries(ref)
        if (
            min_col is None
            or min_row is None
            or max_col is None
            or max_row is None
        ):
            continue
        columns = tuple(
            str(column.name).strip()
            for column in table.tableColumns
            if str(getattr(column, "name", "") or "").strip()
        )
        tables.append(
            ExcelTableDef(
                name=str(table.displayName or table.name or "").strip(),
                sheet=sheet_name,
                min_row=int(min_row),
                min_col=int(min_col),
                max_row=int(max_row),
                max_col=int(max_col),
                header_row_count=int(table.headerRowCount or 0),
                totals_row_count=int(table.totalsRowCount or 0),
                columns=columns,
            )
        )
    return [table for table in tables if table.name]


def _defined_names(workbook) -> list[DefinedNameDef]:
    names: list[DefinedNameDef] = []
    sheet_names = list(workbook.sheetnames)
    defined = getattr(workbook, "defined_names", None)
    if defined is None:
        return names
    values = defined.values() if hasattr(defined, "values") else []
    for item in values:
        name = str(getattr(item, "name", "") or "").strip()
        attr_text = str(getattr(item, "attr_text", "") or "").strip()
        if not name:
            continue
        local_sheet_id = getattr(item, "localSheetId", None)
        sheet: str | None = None
        if local_sheet_id is not None and 0 <= int(local_sheet_id) < len(sheet_names):
            sheet = sheet_names[int(local_sheet_id)]
        names.append(
            DefinedNameDef(
                name=name,
                sheet=sheet,
                attr_text=attr_text,
                unresolved=not attr_text,
            )
        )
    return names
