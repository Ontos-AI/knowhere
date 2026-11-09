# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportOptionalOperand=false, reportOptionalSubscript=false, reportReturnType=false, reportOperatorIssue=false, reportIndexIssue=false, reportAssignmentType=false, reportGeneralTypeIssues=false
from __future__ import annotations

import numpy as np
import pandas as pd
from loguru import logger


def parse_tb_keywords(table_frame: pd.DataFrame) -> str:
    table_keywords: list[str] = []
    for column in table_frame.columns.tolist():
        column_text = str(column).strip()
        if (
            not column_text
            or column_text == "nan"
            or "Unnamed" in column_text
            or column_text in table_keywords
        ):
            continue
        table_keywords.append(column_text)
    return ";".join(table_keywords)


def parse_tb_contents(table_frame: pd.DataFrame) -> str:
    rendered_frame = table_frame.astype(object).where(table_frame.notna(), "")
    return rendered_frame.to_html(
        index=False,
        header=True,
        na_rep="—",
        classes="table table-striped",
        escape=False,
        border=0,
        justify="center",
    ).replace("\n", "")


def postprocess_tb(table_frame: pd.DataFrame) -> pd.DataFrame:
    table_frame = table_frame.dropna(how="all")
    cols_to_drop: list[int] = []
    for column_index, column in enumerate(table_frame.columns):
        if table_frame.iloc[:, column_index].isna().all() and not _meaningful_header(
            column
        ):
            cols_to_drop.append(column_index)
    if cols_to_drop:
        cols_to_keep = [
            index
            for index in range(len(table_frame.columns))
            if index not in cols_to_drop
        ]
        table_frame = table_frame.iloc[:, cols_to_keep]
    logger.debug(f"Dropped {len(cols_to_drop)} empty columns")
    table_frame = table_frame.reset_index(drop=True)

    table_frame.columns = [
        str(column).replace("\n", "") for column in table_frame.columns
    ]
    table_frame.columns = [
        np.nan if "Unnamed" in str(column) else column
        for column in table_frame.columns
    ]
    return table_frame.map(
        lambda value: value.replace("\n", "") if isinstance(value, str) else value
    )


def _meaningful_header(column: object) -> bool:
    text = str(column).strip()
    return bool(text) and text not in {"None", "nan", "NaN"}
