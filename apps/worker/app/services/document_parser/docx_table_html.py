from __future__ import annotations

from typing import Any

from docx.table import Table as DocxTable


def table2html(table: DocxTable, cell_image_map: dict | None = None) -> str:
    """Convert a DOCX table to HTML with colspan, rowspan, and nested tables."""

    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

    def get_cell_vmerge(cell: Any) -> str | None:
        tc = cell._tc
        tc_pr = tc.find(".//w:tcPr", namespaces=namespace)
        if tc_pr is None:
            return None

        v_merge = tc_pr.find(".//w:vMerge", namespaces=namespace)
        if v_merge is None:
            return None

        val = v_merge.get(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val"
        )
        return val if val else "continue"

    row_count = len(table.rows)
    if row_count == 0:
        return "<table border='1'></table>"

    grid = []
    for row in table.rows:
        row_data = []
        previous_tc_id = None
        for cell in row.cells:
            tc_id = id(cell._tc)
            is_new = tc_id != previous_tc_id
            row_data.append((tc_id, cell, is_new))
            previous_tc_id = tc_id
        grid.append(row_data)

    column_count = max(len(row) for row in grid) if grid else 0
    colspan_grid = [[0] * column_count for _ in range(row_count)]

    for row_idx in range(row_count):
        row_len = len(grid[row_idx])
        col_idx = 0
        while col_idx < row_len:
            tc_id = grid[row_idx][col_idx][0]
            span = 1
            while (
                col_idx + span < row_len
                and grid[row_idx][col_idx + span][0] == tc_id
            ):
                span += 1
            colspan_grid[row_idx][col_idx] = span
            col_idx += span

    rowspan_grid = [[1] * column_count for _ in range(row_count)]

    for col_idx in range(column_count):
        row_idx = 0
        while row_idx < row_count:
            if col_idx >= len(grid[row_idx]):
                row_idx += 1
                continue

            cell = grid[row_idx][col_idx][1]
            vmerge = get_cell_vmerge(cell)

            if vmerge == "restart":
                span = 1
                while row_idx + span < row_count:
                    if col_idx >= len(grid[row_idx + span]):
                        break
                    next_cell = grid[row_idx + span][col_idx][1]
                    next_vmerge = get_cell_vmerge(next_cell)
                    if next_vmerge == "continue":
                        span += 1
                    else:
                        break
                rowspan_grid[row_idx][col_idx] = span
                row_idx += span
            elif vmerge == "continue":
                rowspan_grid[row_idx][col_idx] = 0
                row_idx += 1
            else:
                row_idx += 1

    html_parts = ["<table border='1'>"]

    for row_idx in range(row_count):
        html_parts.append("<tr>")
        col_idx = 0
        unique_col_idx = 0

        while col_idx < len(grid[row_idx]):
            _, cell, is_new = grid[row_idx][col_idx]

            if not is_new:
                col_idx += 1
                continue

            rowspan = rowspan_grid[row_idx][col_idx]
            if rowspan == 0:
                unique_col_idx += 1
                col_idx += 1
                continue

            colspan = colspan_grid[row_idx][col_idx]

            if cell.tables:
                content = "".join(table2html(nested_table) for nested_table in cell.tables)
            else:
                content = cell.text.strip().replace("\n", "<br/>")

            if cell_image_map:
                image_description = cell_image_map.get((row_idx, unique_col_idx))
                if image_description:
                    content += f"<br/><em>{image_description}</em>"

            attrs = []
            if colspan > 1:
                attrs.append(f'colspan="{colspan}"')
            if rowspan > 1:
                attrs.append(f'rowspan="{rowspan}"')

            attr_str = " " + " ".join(attrs) if attrs else ""
            html_parts.append(f"<td{attr_str}>{content}</td>")

            unique_col_idx += 1
            col_idx += colspan

        html_parts.append("</tr>")

    html_parts.append("</table>")
    return "".join(html_parts)
