from __future__ import annotations

from typing import List, Union

import pandas as pd


def render_multiindex_thead(columns: pd.MultiIndex, escape: bool = False) -> str:
    """Convert MultiIndex columns to an HTML thead with colspan and rowspan."""
    import html as html_lib

    level_count = columns.nlevels
    column_count = len(columns)

    grid = []
    for level in range(level_count):
        row = [columns.get_level_values(level)[col] for col in range(column_count)]
        grid.append(row)

    colspan = [[1] * column_count for _ in range(level_count)]

    for level in range(level_count):
        col = 0
        while col < column_count:
            span = 1
            while col + span < column_count and grid[level][col] == grid[level][col + span]:
                parent_match = True
                for parent_level in range(level):
                    if grid[parent_level][col] != grid[parent_level][col + span]:
                        parent_match = False
                        break
                if parent_match:
                    span += 1
                else:
                    break
            colspan[level][col] = span
            col += span

    rowspan = [[1] * column_count for _ in range(level_count)]

    for col in range(column_count):
        level = 0
        while level < level_count:
            span = 1
            while level + span < level_count:
                if (
                    grid[level][col] == grid[level + span][col]
                    and colspan[level][col] == colspan[level + span][col]
                ):
                    span += 1
                else:
                    break
            rowspan[level][col] = span
            level += span

    covered = [[False] * column_count for _ in range(level_count)]
    html_parts = ["<thead>"]

    for level in range(level_count):
        html_parts.append('<tr style="text-align: center;">')
        col = 0
        while col < column_count:
            if covered[level][col]:
                col += 1
                continue

            val = grid[level][col]
            val_str = str(val) if val is not None else ""
            if escape:
                val_str = html_lib.escape(val_str)

            column_span = colspan[level][col]
            row_span = rowspan[level][col]

            for row_offset in range(row_span):
                for column_offset in range(column_span):
                    if row_offset > 0 or column_offset > 0:
                        if (
                            level + row_offset < level_count
                            and col + column_offset < column_count
                        ):
                            covered[level + row_offset][col + column_offset] = True

            attrs = []
            if column_span > 1:
                attrs.append(f'colspan="{column_span}"')
            if row_span > 1:
                attrs.append(f'rowspan="{row_span}"')

            attr_str = " " + " ".join(attrs) if attrs else ""
            html_parts.append(f"<th{attr_str}>{val_str}</th>")

            col += column_span

        html_parts.append("</tr>")

    html_parts.append("</thead>")
    return "".join(html_parts)


def render_tbody_with_row_headers(
    tb_df: pd.DataFrame,
    row_header_cols: int = 0,
    na_rep: str = "—",
    escape: bool = False,
) -> str:
    """Render a DataFrame body with optional row-header cells and merged spans."""
    import html as html_lib

    if row_header_cols <= 0:
        html_parts = ["<tbody>"]
        for _, row in tb_df.iterrows():
            html_parts.append("<tr>")
            for val in row:
                val_str = na_rep if pd.isna(val) else str(val)
                if escape:
                    val_str = html_lib.escape(val_str)
                html_parts.append(f"<td>{val_str}</td>")
            html_parts.append("</tr>")
        html_parts.append("</tbody>")
        return "".join(html_parts)

    row_count = len(tb_df)
    column_count = len(tb_df.columns)

    if row_count == 0:
        return "<tbody></tbody>"

    grid = []
    for row_idx in range(row_count):
        row_values = []
        for col_idx in range(row_header_cols):
            val = tb_df.iloc[row_idx, col_idx]
            val = na_rep if pd.isna(val) else str(val)
            row_values.append(val)
        grid.append(row_values)

    colspan = [[1] * row_header_cols for _ in range(row_count)]

    for row_idx in range(row_count):
        col_idx = 0
        while col_idx < row_header_cols:
            span = 1
            while (
                col_idx + span < row_header_cols
                and grid[row_idx][col_idx] == grid[row_idx][col_idx + span]
            ):
                span += 1
            colspan[row_idx][col_idx] = span
            col_idx += span

    rowspan = [[1] * row_header_cols for _ in range(row_count)]

    col_idx = 0
    while col_idx < row_header_cols:
        row_idx = 0
        while row_idx < row_count:
            if col_idx > 0 and grid[row_idx][col_idx] == grid[row_idx][col_idx - 1]:
                row_idx += 1
                continue

            current_colspan = colspan[row_idx][col_idx]
            span = 1

            while row_idx + span < row_count:
                if grid[row_idx][col_idx] != grid[row_idx + span][col_idx]:
                    break
                if colspan[row_idx + span][col_idx] != current_colspan:
                    break

                parent_match = True
                for parent_col in range(col_idx):
                    if grid[row_idx][parent_col] != grid[row_idx + span][parent_col]:
                        parent_match = False
                        break
                if parent_match:
                    span += 1
                else:
                    break

            rowspan[row_idx][col_idx] = span
            row_idx += span
        col_idx += 1

    covered = [[False] * row_header_cols for _ in range(row_count)]

    for row_idx in range(row_count):
        col_idx = 0
        while col_idx < row_header_cols:
            column_span = colspan[row_idx][col_idx]
            for offset in range(1, column_span):
                if col_idx + offset < row_header_cols:
                    covered[row_idx][col_idx + offset] = True
            col_idx += column_span

    for row_idx in range(row_count):
        for col_idx in range(row_header_cols):
            if covered[row_idx][col_idx]:
                continue
            row_span = rowspan[row_idx][col_idx]
            for offset in range(1, row_span):
                if row_idx + offset < row_count:
                    column_span = colspan[row_idx][col_idx]
                    for column_offset in range(column_span):
                        if col_idx + column_offset < row_header_cols:
                            covered[row_idx + offset][col_idx + column_offset] = True

    html_parts = ["<tbody>"]

    for row_idx in range(row_count):
        html_parts.append("<tr>")

        for col_idx in range(row_header_cols):
            if covered[row_idx][col_idx]:
                continue

            val_str = grid[row_idx][col_idx]
            if escape:
                val_str = html_lib.escape(val_str)

            row_span = rowspan[row_idx][col_idx]
            column_span = colspan[row_idx][col_idx]

            attrs = []
            if row_span > 1:
                attrs.append(f'rowspan="{row_span}"')
            if column_span > 1:
                attrs.append(f'colspan="{column_span}"')

            attr_str = " " + " ".join(attrs) if attrs else ""
            html_parts.append(f'<th scope="row"{attr_str}>{val_str}</th>')

        for col_idx in range(row_header_cols, column_count):
            val = tb_df.iloc[row_idx, col_idx]
            val_str = na_rep if pd.isna(val) else str(val)
            if escape:
                val_str = html_lib.escape(val_str)
            html_parts.append(f"<td>{val_str}</td>")

        html_parts.append("</tr>")

    html_parts.append("</tbody>")
    return "".join(html_parts)


def df2html(
    tb_df: pd.DataFrame,
    *,
    index: bool = False,
    classes: Union[str, List[str], None] = "table table-striped",
    na_rep: str = "—",
    escape: bool = False,
    row_header_cols: int = 0,
) -> str:
    """Convert a DataFrame to an HTML table."""
    class_str = (
        classes if isinstance(classes, str) else " ".join(classes) if classes else ""
    )

    if isinstance(tb_df.columns, pd.MultiIndex):
        thead_html = render_multiindex_thead(tb_df.columns, escape=escape)
        tbody_html = render_tbody_with_row_headers(
            tb_df, row_header_cols, na_rep, escape
        )
        return f'<table class="dataframe {class_str}">{thead_html}{tbody_html}</table>'

    if row_header_cols <= 0:
        table_html = tb_df.to_html(
            index=index,
            na_rep=na_rep,
            classes=classes,
            escape=escape,
            border=0,
            justify="center",
        )
        return table_html.replace("\n", "")

    html_parts = [f'<table class="dataframe {class_str}">']
    html_parts.append("<thead>")
    html_parts.append('<tr style="text-align: center;">')
    for col in tb_df.columns:
        col_str = str(col) if col is not None else ""
        if escape:
            import html as html_lib

            col_str = html_lib.escape(col_str)
        html_parts.append(f"<th>{col_str}</th>")
    html_parts.append("</tr>")
    html_parts.append("</thead>")

    tbody_html = render_tbody_with_row_headers(tb_df, row_header_cols, na_rep, escape)
    html_parts.append(tbody_html)
    html_parts.append("</table>")
    return "".join(html_parts)
