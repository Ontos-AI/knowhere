from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from openpyxl.formula.tokenizer import Token, Tokenizer
from openpyxl.utils import column_index_from_string
from openpyxl.utils.cell import ABSOLUTE_RE, SHEETRANGE_RE, range_boundaries

from app.services.document_parser.formats.excel.workbook_grid import (
    DefinedNameDef,
    ExcelTableDef,
    Region,
    WorkbookGrid,
)

_UNRESOLVED_FUNCTIONS = frozenset({"INDIRECT", "OFFSET"})


@dataclass(frozen=True)
class CellRange:
    sheet: str
    min_row: int
    min_col: int
    max_row: int
    max_col: int


@dataclass(frozen=True)
class FormulaNode:
    sheet: str
    row: int
    col: int
    formula: str
    refs: tuple[CellRange, ...]
    unresolved: bool


def range_hits_region(ref: CellRange, sheet_name: str, region: Region) -> bool:
    """Whether a formula reference intersects ``region`` on ``sheet_name``."""
    if ref.sheet != sheet_name:
        return False
    return not (
        ref.max_row < region.min_row
        or region.max_row < ref.min_row
        or ref.max_col < region.min_col
        or region.max_col < ref.min_col
    )


def build_formula_graph(workbook: WorkbookGrid) -> list[FormulaNode]:
    names = list(workbook.defined_names)
    tables = [table for sheet in workbook.sheets for table in sheet.excel_tables]
    bounds = workbook.sheet_bounds()
    nodes: list[FormulaNode] = []
    for sheet in workbook.sheets:
        for cell in sheet.cells.values():
            if not cell.formula:
                continue
            refs, unresolved = _analyze_formula(
                cell.formula,
                current_sheet=sheet.name,
                names=names,
                tables=tables,
                sheet_bounds=bounds,
            )
            nodes.append(
                FormulaNode(
                    sheet=sheet.name,
                    row=cell.row,
                    col=cell.col,
                    formula=cell.formula,
                    refs=refs,
                    unresolved=unresolved,
                )
            )
    return nodes


def parse_formula_refs(
    formula: str,
    current_sheet: str,
    names: list[DefinedNameDef],
    *,
    tables: Sequence[ExcelTableDef] = (),
    sheet_bounds: Mapping[str, tuple[int, int]] | None = None,
) -> tuple[CellRange, ...]:
    refs, _unresolved = _analyze_formula(
        formula,
        current_sheet=current_sheet,
        names=names,
        tables=tables,
        sheet_bounds=sheet_bounds or {},
    )
    return refs


def _analyze_formula(
    formula: str,
    *,
    current_sheet: str,
    names: Sequence[DefinedNameDef],
    tables: Sequence[ExcelTableDef],
    sheet_bounds: Mapping[str, tuple[int, int]],
) -> tuple[tuple[CellRange, ...], bool]:
    try:
        tokens = Tokenizer(formula).items
    except Exception:
        return (), True

    refs: list[CellRange] = []
    unresolved = False
    seen: set[tuple[str, int, int, int, int]] = set()

    for token in tokens:
        if token.type == Token.FUNC and token.subtype == Token.OPEN:
            if _function_name(token.value).upper() in _UNRESOLVED_FUNCTIONS:
                unresolved = True
            continue
        if token.type == Token.OPERAND and token.subtype == Token.ERROR:
            unresolved = True
            continue
        if token.type != Token.OPERAND or token.subtype != Token.RANGE:
            continue
        parsed, token_unresolved = _resolve_operand(
            str(token.value),
            current_sheet=current_sheet,
            names=names,
            tables=tables,
            sheet_bounds=sheet_bounds,
            visiting=set(),
        )
        if token_unresolved:
            unresolved = True
        for item in parsed:
            key = (item.sheet, item.min_row, item.min_col, item.max_row, item.max_col)
            if key in seen:
                continue
            seen.add(key)
            refs.append(item)
    return tuple(refs), unresolved


def _resolve_operand(
    token: str,
    *,
    current_sheet: str,
    names: Sequence[DefinedNameDef],
    tables: Sequence[ExcelTableDef],
    sheet_bounds: Mapping[str, tuple[int, int]],
    visiting: set[str],
) -> tuple[tuple[CellRange, ...], bool]:
    text = token.strip()
    if not text:
        return (), True
    if "[" in text:
        table_refs, ok = _resolve_table_ref(
            text,
            current_sheet=current_sheet,
            tables=tables,
        )
        return table_refs, not ok
    coordinate = _resolve_coordinate(
        text, current_sheet=current_sheet, sheet_bounds=sheet_bounds
    )
    if coordinate is not None:
        return (coordinate,), False
    return _resolve_defined_name(
        text,
        current_sheet=current_sheet,
        names=names,
        tables=tables,
        sheet_bounds=sheet_bounds,
        visiting=visiting,
    )


def _resolve_coordinate(
    token: str,
    *,
    current_sheet: str,
    sheet_bounds: Mapping[str, tuple[int, int]],
) -> CellRange | None:
    sheet_name, cells = _split_sheet_ref(token)
    sheet = sheet_name or current_sheet
    parsed = _cells_to_range(
        cells, sheet=sheet, bounds=sheet_bounds.get(sheet)
    )
    return parsed


def _split_sheet_ref(token: str) -> tuple[str | None, str]:
    match = SHEETRANGE_RE.match(token)
    if match is None:
        return None, token
    quoted = match.group("quoted")
    notquoted = match.group("notquoted")
    if quoted is not None:
        sheet = quoted.replace("''", "'")
    else:
        sheet = notquoted or None
    cells = match.group("cells") or token
    return sheet, cells


def _cells_to_range(
    cells: str,
    *,
    sheet: str,
    bounds: tuple[int, int] | None,
) -> CellRange | None:
    sheet_max_row, sheet_max_col = bounds if bounds is not None else (None, None)
    match = ABSOLUTE_RE.match(cells)
    if match is not None:
        groups = match.groupdict()
        min_col_text = groups.get("min_col")
        max_col_text = groups.get("max_col") or min_col_text
        min_row_text = groups.get("min_row")
        max_row_text = groups.get("max_row") or min_row_text
        min_col = (
            column_index_from_string(min_col_text) if min_col_text else 1
        )
        min_row = int(min_row_text) if min_row_text else 1
        if min_col_text is None:
            if sheet_max_col is None:
                return None
            max_col_n = sheet_max_col
        elif max_col_text:
            max_col_n = column_index_from_string(max_col_text)
        else:
            max_col_n = min_col
        if min_row_text is None:
            if sheet_max_row is None:
                return None
            max_row_n = sheet_max_row
        elif max_row_text:
            max_row_n = int(max_row_text)
        else:
            max_row_n = min_row
        return CellRange(
            sheet=sheet,
            min_row=min(min_row, max_row_n),
            min_col=min(min_col, max_col_n),
            max_row=max(min_row, max_row_n),
            max_col=max(min_col, max_col_n),
        )
    try:
        min_col, min_row, max_col_n, max_row_n = range_boundaries(cells)
    except Exception:
        return None
    if min_col is None:
        if sheet_max_col is None:
            return None
        min_col = 1
        max_col_n = sheet_max_col if max_col_n is None else max_col_n
    elif max_col_n is None:
        max_col_n = min_col
    if min_row is None:
        if sheet_max_row is None:
            return None
        min_row = 1
        max_row_n = sheet_max_row if max_row_n is None else max_row_n
    elif max_row_n is None:
        max_row_n = min_row
    return CellRange(
        sheet=sheet,
        min_row=int(min_row),
        min_col=int(min_col),
        max_row=int(max_row_n),
        max_col=int(max_col_n),
    )


def _resolve_defined_name(
    token: str,
    *,
    current_sheet: str,
    names: Sequence[DefinedNameDef],
    tables: Sequence[ExcelTableDef],
    sheet_bounds: Mapping[str, tuple[int, int]],
    visiting: set[str],
) -> tuple[tuple[CellRange, ...], bool]:
    key = token.casefold()
    if key in visiting:
        return (), True
    match = _lookup_name(token, current_sheet=current_sheet, names=names)
    if match is None:
        return (), True
    if match.unresolved or not match.attr_text:
        return (), True
    visiting.add(key)
    refs: list[CellRange] = []
    unresolved = False
    for part in _split_union(match.attr_text.lstrip("=")):
        parsed, part_unresolved = _resolve_operand(
            part,
            current_sheet=match.sheet or current_sheet,
            names=names,
            tables=tables,
            sheet_bounds=sheet_bounds,
            visiting=visiting,
        )
        if part_unresolved:
            unresolved = True
        refs.extend(parsed)
    visiting.remove(key)
    return tuple(refs), unresolved or not refs


def _lookup_name(
    token: str, *, current_sheet: str, names: Sequence[DefinedNameDef]
) -> DefinedNameDef | None:
    key = token.casefold()
    local: DefinedNameDef | None = None
    global_name: DefinedNameDef | None = None
    for item in names:
        if item.name.casefold() != key:
            continue
        if item.sheet == current_sheet:
            local = item
            break
        if item.sheet is None:
            global_name = item
    return local or global_name


def _resolve_table_ref(
    token: str,
    *,
    current_sheet: str,
    tables: Sequence[ExcelTableDef],
) -> tuple[tuple[CellRange, ...], bool]:
    name, spec = _split_table_ref(token)
    if name is None or spec is None:
        return (), False
    table = _lookup_table(name, current_sheet=current_sheet, tables=tables)
    if table is None or spec.startswith("#"):
        return (), False
    body_start = table.min_row + table.header_row_count
    body_end = table.max_row - table.totals_row_count
    column_index = _table_column_index(table, spec)
    if column_index is None or body_start > body_end:
        return (), False
    col = table.min_col + column_index
    return (
        CellRange(
            sheet=table.sheet,
            min_row=body_start,
            min_col=col,
            max_row=body_end,
            max_col=col,
        ),
    ), True


def _split_table_ref(token: str) -> tuple[str | None, str | None]:
    if token.count("[") != 1 or not token.endswith("]"):
        return None, None
    name, rest = token.split("[", 1)
    spec = rest[:-1].strip()
    if spec.startswith("[") and spec.endswith("]"):
        return None, None
    table_name = name.strip()
    if not table_name or not spec or ":" in spec:
        return None, None
    return table_name, spec


def _lookup_table(
    name: str, *, current_sheet: str, tables: Sequence[ExcelTableDef]
) -> ExcelTableDef | None:
    key = name.casefold()
    local: ExcelTableDef | None = None
    any_table: ExcelTableDef | None = None
    for table in tables:
        if table.name.casefold() != key:
            continue
        if table.sheet == current_sheet:
            local = table
            break
        if any_table is None:
            any_table = table
    return local or any_table


def _table_column_index(table: ExcelTableDef, column_name: str) -> int | None:
    key = column_name.casefold()
    for index, name in enumerate(table.columns):
        if name.casefold() == key:
            return index
    return None


def _function_name(token_value: str) -> str:
    name = token_value[:-1] if token_value.endswith("(") else token_value
    xl_prefix = "_xlfn."
    if name.lower().startswith(xl_prefix):
        name = name[len(xl_prefix) :]
    return name.strip()


def _split_union(text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    for char in text:
        if char == "'":
            quoted = not quoted
            current.append(char)
            continue
        if char == "," and not quoted:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(char)
    part = "".join(current).strip()
    if part:
        parts.append(part)
    return parts
