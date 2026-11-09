# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportOptionalOperand=false, reportOptionalSubscript=false, reportReturnType=false, reportOperatorIssue=false, reportIndexIssue=false, reportAssignmentType=false, reportGeneralTypeIssues=false
from __future__ import annotations

import numpy as np
import pandas as pd
from app.services.document_parser.tables.dataframe_html_renderer import df2html
from loguru import logger

from shared.utils.text_utils import remove_duplicates_orderkept


def parse_tb_keywords(table_frame: pd.DataFrame) -> str:
    def parse_single_level(columns: list[object], keywords: list[str]) -> list[str]:
        for column in columns:
            column_text = str(column)
            if column_text not in keywords:
                keywords.append(column_text)
        return list({keyword.strip() for keyword in keywords})

    table_keywords: list[str] = []
    if isinstance(table_frame.columns, pd.MultiIndex):
        multi_columns = table_frame.columns
        columns_frame = pd.DataFrame(
            multi_columns.tolist(),
            columns=[f"level_{i}" for i in range(multi_columns.nlevels)],
        )
        for level_index in range(multi_columns.nlevels):
            level_keywords: list[str] = []
            level_keywords = parse_single_level(
                columns_frame[f"level_{level_index}"].tolist(),
                level_keywords,
            )
            table_keywords.extend(level_keywords)
    else:
        table_keywords = parse_single_level(table_frame.columns.tolist(), table_keywords)

    table_keywords = remove_duplicates_orderkept(table_keywords)
    table_keywords = [
        keyword
        for keyword in table_keywords
        if isinstance(keyword, str)
        and keyword.strip()
        and keyword.strip() != "nan"
        and "Unnamed" not in keyword
    ]
    return ";".join(table_keywords)


def parse_tb_contents(
    table_frame: pd.DataFrame,
    row_header_cols: int = 0,
    include_header: bool = True,
) -> str:
    rendered_frame = table_frame.astype(object).where(table_frame.notna(), "")
    return df2html(
        rendered_frame,
        row_header_cols=row_header_cols,
        header=include_header,
    )


def postprocess_tb(table_frame: pd.DataFrame, drop: bool = False) -> pd.DataFrame:
    if drop:
        was_range_index = isinstance(table_frame.index, pd.RangeIndex)
        table_frame = table_frame.dropna(how="all")

        if was_range_index:
            table_frame = table_frame.reset_index(drop=True)

        cols_to_drop: list[int] = []
        for column_index, column in enumerate(table_frame.columns):
            if table_frame.iloc[:, column_index].isna().all():
                has_meaningful_header = False
                if isinstance(column, tuple):
                    for level in column:
                        if (
                            level
                            and str(level).strip()
                            and str(level).strip() not in ["None", "nan", "NaN"]
                        ):
                            has_meaningful_header = True
                            break
                elif (
                    column
                    and str(column).strip()
                    and str(column).strip() not in ["None", "nan", "NaN"]
                ):
                    has_meaningful_header = True

                if not has_meaningful_header:
                    cols_to_drop.append(column_index)

        if cols_to_drop:
            cols_to_keep = [
                index
                for index in range(len(table_frame.columns))
                if index not in cols_to_drop
            ]
            table_frame = table_frame.iloc[:, cols_to_keep]
            column_headers = table_frame.attrs.get("column_headers")
            row_header_cols = int(table_frame.attrs.get("row_header_col_count") or 0)
            if isinstance(column_headers, (list, tuple)) and len(column_headers) >= row_header_cols:
                kept_set = set(cols_to_keep)
                head_prefix = list(column_headers[:row_header_cols])
                head_rest = [
                    header
                    for idx, header in enumerate(column_headers[row_header_cols:])
                    if idx in kept_set
                ]
                table_frame.attrs["column_headers"] = tuple(head_prefix + head_rest)

        logger.debug(f"Dropped {len(cols_to_drop)} empty columns")

        if not isinstance(table_frame.index, pd.RangeIndex):
            was_multiindex = isinstance(table_frame.columns, pd.MultiIndex)
            column_level_count = table_frame.columns.nlevels if was_multiindex else 1
            existing_column_set = set(table_frame.columns)

            def make_padded(name: object) -> object:
                if was_multiindex:
                    return tuple([name] * column_level_count)
                return name

            if isinstance(table_frame.index, pd.MultiIndex):
                seen_counts: dict[object, int] = {}
                deduped_names: list[object | None] = []
                for name in table_frame.index.names:
                    if name is None:
                        deduped_names.append(None)
                        continue
                    padded = make_padded(name)
                    if padded in existing_column_set or name in seen_counts:
                        deduped_names.append(None)
                    else:
                        deduped_names.append(name)
                    seen_counts[name] = seen_counts.get(name, 0) + 1
                table_frame.index.names = deduped_names
            elif hasattr(table_frame.index, "name") and table_frame.index.name is not None:
                padded = make_padded(table_frame.index.name)
                if padded in existing_column_set:
                    table_frame.index.name = None

            row_header_tuples: list[tuple[str, ...]] = list(
                table_frame.attrs.get("row_header_column_tuples") or []
            )
            index_level_count = (
                table_frame.index.nlevels
                if isinstance(table_frame.index, pd.MultiIndex)
                else 1
            )
            table_frame = table_frame.reset_index()

            if was_multiindex:
                new_columns = []
                for col_idx, column in enumerate(table_frame.columns):
                    if col_idx < index_level_count and col_idx < len(row_header_tuples):
                        padded_tuple = tuple(
                            list(row_header_tuples[col_idx])[:column_level_count]
                            + [""] * max(0, column_level_count - len(row_header_tuples[col_idx]))
                        )
                        new_columns.append(padded_tuple)
                    elif isinstance(column, str) and (
                        column.startswith("level_") or column == "index"
                    ):
                        new_columns.append(tuple([""] * column_level_count))
                    elif isinstance(column, tuple) and len(column) == column_level_count:
                        if all(c == "" for c in column[1:]) and column[0] != "":
                            new_columns.append(tuple([column[0]] * column_level_count))
                        else:
                            new_columns.append(column)
                    else:
                        new_columns.append(column)
                table_frame.columns = pd.MultiIndex.from_tuples(new_columns)
            else:
                new_columns = []
                for col_idx, column in enumerate(table_frame.columns):
                    if col_idx < index_level_count and col_idx < len(row_header_tuples) and row_header_tuples[col_idx]:
                        new_columns.append(row_header_tuples[col_idx][-1])
                    elif isinstance(column, str) and (
                        column.startswith("level_") or column == "index"
                    ):
                        new_columns.append("")
                    else:
                        new_columns.append(column)
                table_frame.columns = new_columns
        else:
            table_frame.reset_index(drop=True, inplace=True)

    if isinstance(table_frame.columns, pd.MultiIndex):
        new_levels = []
        for level_index in range(table_frame.columns.nlevels):
            level_values = table_frame.columns.get_level_values(level_index)
            cleaned = [
                str(value).replace("\n", "") if value is not None else ""
                for value in level_values
            ]
            new_levels.append(cleaned)
        table_frame.columns = pd.MultiIndex.from_arrays(
            new_levels,
            names=table_frame.columns.names,
        )

        new_levels = []
        for level_index in range(table_frame.columns.nlevels):
            level_values = table_frame.columns.get_level_values(level_index)
            cleaned = [np.nan if "Unnamed" in str(value) else value for value in level_values]
            new_levels.append(cleaned)
        table_frame.columns = pd.MultiIndex.from_arrays(
            new_levels,
            names=table_frame.columns.names,
        )
    else:
        table_frame.columns = [
            str(column).replace("\n", "") for column in table_frame.columns
        ]
        table_frame.columns = [
            np.nan if "Unnamed" in str(column) else column
            for column in table_frame.columns
        ]

    # Keep native temporals (incl. NaT); stringify at df2html / to_html only.
    return table_frame.map(
        lambda value: value.replace("\n", "") if isinstance(value, str) else value
    )

