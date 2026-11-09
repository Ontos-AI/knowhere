from __future__ import annotations

import html

from openpyxl.utils import get_column_letter

from app.services.document_parser.formats.excel.header_detect import HeaderSpec
from app.services.document_parser.formats.excel.workbook_grid import (
    GridCell,
    Region,
    SheetGrid,
    cell_has_value_or_formula,
    display_value,
    expand_visual_grid,
)


def visible_columns(sheet: SheetGrid, region: Region) -> tuple[int, ...]:
    """Columns rendered in HTML: keep visible columns; drop hidden empties."""
    columns: list[int] = []
    for col in range(region.min_col, region.max_col + 1):
        if col in sheet.hidden_columns and not _column_has_value_or_formula(
            sheet, region, col
        ):
            continue
        columns.append(col)
    return tuple(columns)


def render_table_html(sheet: SheetGrid, region: Region, headers: HeaderSpec) -> str:
    """Render one table region as HTML with real merge spans and formula attrs."""
    visible = visible_columns(sheet, region)
    if not visible or region.max_row < region.min_row:
        return ""
    expanded = expand_visual_grid(sheet, region)
    if not expanded:
        return ""

    range_text = (
        f"{get_column_letter(region.min_col)}{region.min_row}:"
        f"{get_column_letter(region.max_col)}{region.max_row}"
    )
    parts = [
        "<table",
        f' data-sheet="{_attr(sheet.name)}"',
        f' data-range="{_attr(range_text)}"',
        ">",
    ]
    caption = (headers.caption or "").strip()
    if caption:
        parts.append(f"<caption>{html.escape(caption)}</caption>")

    header_row_count = headers.header_row_count
    if header_row_count > 0:
        thead_end = region.min_row + header_row_count - 1
        parts.append("<thead>")
        for excel_row in range(region.min_row, thead_end + 1):
            parts.append(
                _render_tr(
                    expanded=expanded,
                    excel_row=excel_row,
                    region=region,
                    visible=visible,
                    section_min_row=region.min_row,
                    section_max_row=thead_end,
                    row_header_col_count=headers.row_header_col_count,
                    header_section=True,
                )
            )
        parts.append("</thead>")

    parts.append("<tbody>")
    body_start = region.min_row + header_row_count
    if body_start <= region.max_row:
        for excel_row in range(body_start, region.max_row + 1):
            parts.append(
                _render_tr(
                    expanded=expanded,
                    excel_row=excel_row,
                    region=region,
                    visible=visible,
                    section_min_row=body_start,
                    section_max_row=region.max_row,
                    row_header_col_count=headers.row_header_col_count,
                    header_section=False,
                )
            )
    parts.append("</tbody>")
    parts.append("<tfoot></tfoot>")
    parts.append("</table>")
    return "".join(parts)


def _column_has_value_or_formula(
    sheet: SheetGrid, region: Region, col: int
) -> bool:
    for row in range(region.min_row, region.max_row + 1):
        if cell_has_value_or_formula(sheet, row, col):
            return True
    return False


def _render_tr(
    *,
    expanded: list[list[GridCell | None]],
    excel_row: int,
    region: Region,
    visible: tuple[int, ...],
    section_min_row: int,
    section_max_row: int,
    row_header_col_count: int,
    header_section: bool,
) -> str:
    rel_row = excel_row - region.min_row
    row_cells = expanded[rel_row] if 0 <= rel_row < len(expanded) else []
    parts = ["<tr>"]
    for col in visible:
        rel_col = col - region.min_col
        cell = row_cells[rel_col] if 0 <= rel_col < len(row_cells) else None
        if not _emit_cell(
            cell,
            excel_row=excel_row,
            col=col,
            section_min_row=section_min_row,
            section_max_row=section_max_row,
            visible=visible,
        ):
            continue
        rowspan, colspan = _clipped_spans(
            cell,
            excel_row=excel_row,
            col=col,
            visible=visible,
            region=region,
            section_max_row=section_max_row,
        )
        is_row_header = (
            not header_section and col < region.min_col + row_header_col_count
        )
        tag = "th" if header_section or is_row_header else "td"
        attrs: list[str] = []
        if is_row_header:
            attrs.append('scope="row"')
        if rowspan > 1:
            attrs.append(f'rowspan="{rowspan}"')
        if colspan > 1:
            attrs.append(f'colspan="{colspan}"')
        if cell is not None and cell.formula:
            attrs.append(f'data-formula="{_attr(cell.formula)}"')
        attr_str = f" {' '.join(attrs)}" if attrs else ""
        parts.append(f"<{tag}{attr_str}>{_cell_text(cell)}</{tag}>")
    parts.append("</tr>")
    return "".join(parts)


def _emit_cell(
    cell: GridCell | None,
    *,
    excel_row: int,
    col: int,
    section_min_row: int,
    section_max_row: int,
    visible: tuple[int, ...],
) -> bool:
    if cell is None or cell.merge_origin is None:
        return True
    origin_row, origin_col = cell.merge_origin
    if section_min_row <= origin_row <= section_max_row:
        return False
    leftover_row = max(origin_row, section_min_row)
    merge_max_col = origin_col + max(cell.colspan, 1) - 1
    leftover_col = next(
        (item for item in visible if origin_col <= item <= merge_max_col),
        None,
    )
    return excel_row == leftover_row and col == leftover_col


def _clipped_spans(
    cell: GridCell | None,
    *,
    excel_row: int,
    col: int,
    visible: tuple[int, ...],
    region: Region,
    section_max_row: int,
) -> tuple[int, int]:
    rowspan = 1 if cell is None else max(cell.rowspan, 1)
    colspan = 1 if cell is None else max(cell.colspan, 1)
    origin_row = excel_row
    origin_col = col
    if cell is not None and cell.merge_origin is not None:
        origin_row, origin_col = cell.merge_origin
    merge_end_row = origin_row + rowspan - 1
    merge_end_col = origin_col + colspan - 1
    clipped_rowspan = (
        min(merge_end_row, section_max_row, region.max_row) - excel_row + 1
    )
    clipped_colspan = sum(
        1
        for item in visible
        if col <= item <= min(merge_end_col, region.max_col)
    )
    return max(clipped_rowspan, 1), max(clipped_colspan, 1)


def _cell_text(cell: GridCell | None) -> str:
    return html.escape(_display_text(display_value(cell)))


def _display_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _attr(value: str) -> str:
    return html.escape(value, quote=True)
