from __future__ import annotations

from typing import List, Optional, Tuple, Union
from typing import cast

import pandas as pd
from openpyxl.utils import get_column_letter
from pandas._typing import Axes
from loguru import logger

from app.services.document_parser.formats.excel.header_detect import (
    SpreadsheetHeaderDetector,
    TableRegion,
    build_region_visual,
    detect_headers,
)
from app.services.document_parser.formats.excel.region_split import (
    classify_sheet_row,
    seed_title_for_region,
    split_sheet_regions,
)
from app.services.document_parser.formats.excel.workbook_grid import (
    Region,
    SheetGrid,
    WorkbookGrid,
    sheet_display_value,
)
from shared.core.exceptions.domain_exceptions import TableParsingException


def parse_excel_structure(
    workbook: WorkbookGrid,
    sheet_name: Optional[str] = None,
    split_subtables: bool = True,
    include_hidden_sheets: bool = False,
    header_detector: SpreadsheetHeaderDetector | None = None,
) -> dict[str, pd.DataFrame]:
    try:
        results: dict[str, pd.DataFrame] = {}
        sheets = workbook.sheet_by_name()
        sheets_to_parse = [sheet_name] if sheet_name else [sheet.name for sheet in workbook.sheets]

        for selected_sheet_name in sheets_to_parse:
            sheet = sheets.get(selected_sheet_name)
            if sheet is None:
                logger.warning(
                    f"Sheet '{selected_sheet_name}' not found in workbook, skipping"
                )
                continue

            if not include_hidden_sheets and sheet.hidden:
                logger.info(
                    f"Sheet '{selected_sheet_name}' is hidden, skipping"
                )
                continue

            if sheet.max_row == 0 or sheet.max_col == 0:
                logger.debug(f"Sheet '{selected_sheet_name}' is empty, skipping")
                continue

            logger.debug(
                f"Sheet '{selected_sheet_name}': found {len(sheet.merged_ranges)} merged cell ranges"
            )

            if split_subtables:
                regions = split_sheet_regions(
                    sheet, defined_names=workbook.defined_names
                )
                logger.debug(
                    f"Sheet '{selected_sheet_name}': {len(regions)} subtables after split"
                )

                for index, region in enumerate(regions):
                    row_range = (region.min_row, region.max_row)
                    col_range = (region.min_col, region.max_col)
                    result = _parse_subtable(
                        sheet,
                        row_range,
                        col_range,
                        header_detector=header_detector,
                    )
                    dataframe = result["df"]
                    spec = result["header_spec"]
                    _apply_subtable_attrs(
                        dataframe,
                        result=result,
                        sheet_name=selected_sheet_name,
                        subtable_index=index + 1,
                        subtable_count=len(regions),
                        seed_title=seed_title_for_region(
                            sheet, region, workbook.defined_names
                        ),
                    )
                    key = selected_sheet_name if index == 0 else f"{selected_sheet_name}_{index + 1}"
                    logger.debug(
                        f"Subtable '{key}': rows={row_range}, cols={col_range}, "
                        f"header_row_count={spec.header_row_count}, "
                        f"row_header_col_count={spec.row_header_col_count}"
                    )
                    results[key] = dataframe
            else:
                row_range = (1, sheet.max_row)
                col_range = (1, sheet.max_col or 1)
                result = _parse_subtable(
                    sheet,
                    row_range,
                    col_range,
                    header_detector=header_detector,
                )
                dataframe = result["df"]
                spec = result["header_spec"]
                _apply_subtable_attrs(
                    dataframe,
                    result=result,
                    sheet_name=selected_sheet_name,
                    subtable_index=1,
                    subtable_count=1,
                    seed_title="",
                )
                logger.debug(
                    f"Sheet '{selected_sheet_name}': "
                    f"header_row_count={spec.header_row_count}, "
                    f"row_header_col_count={spec.row_header_col_count}"
                )
                results[selected_sheet_name] = dataframe

        return results
    except Exception as exc:
        logger.error(f"Error parsing Excel with precision mode: {exc}")
        raise TableParsingException(
            user_message="Failed to parse Excel file headers",
            reason="EXCEL_PRECISION_PARSE_FAILED",
            internal_message=str(exc),
            original_exception=exc,
        ) from exc


def _apply_subtable_attrs(
    dataframe: pd.DataFrame,
    *,
    result: dict,
    sheet_name: str,
    subtable_index: int,
    subtable_count: int,
    seed_title: str,
) -> None:
    spec = result["header_spec"]
    dataframe.attrs["header_row_count"] = spec.header_row_count
    dataframe.attrs["row_header_col_count"] = spec.row_header_col_count
    dataframe.attrs["column_headers"] = spec.column_headers
    dataframe.attrs["row_header_column_tuples"] = result.get("row_header_column_tuples", ())
    dataframe.attrs["caption"] = spec.caption
    dataframe.attrs["excel_range"] = result["excel_range"]
    dataframe.attrs["sheet_name"] = sheet_name
    if subtable_count <= 1:
        subtable_title = ""
    elif seed_title:
        subtable_title = seed_title
    else:
        subtable_title = f"子表 {subtable_index}"
    dataframe.attrs["subtable_title"] = subtable_title


def _build_column_multiindex(
    sheet: SheetGrid,
    header_rows: List[int],
    col_range: Tuple[int, int],
) -> Union[pd.Index, pd.MultiIndex]:
    col_start, col_end = col_range
    levels = []

    for row in header_rows:
        row_values = []
        for col in range(col_start, col_end + 1):
            value = sheet_display_value(sheet, row, col)
            row_values.append(str(value).strip() if value is not None else "")
        levels.append(row_values)

    if len(levels) == 1:
        return pd.Index(levels[0])
    return pd.MultiIndex.from_arrays(levels)


def _build_row_multiindex(
    sheet: SheetGrid,
    header_cols: List[int],
    row_range: Tuple[int, int],
) -> Union[pd.Index, pd.MultiIndex]:
    row_start, row_end = row_range
    levels = []

    for col in header_cols:
        col_values = []
        for row in range(row_start, row_end + 1):
            value = sheet_display_value(sheet, row, col)
            col_values.append(str(value).strip() if value is not None else "")
        levels.append(col_values)

    if len(levels) == 1:
        return pd.Index(levels[0])
    return pd.MultiIndex.from_arrays(levels)


def _strip_leading_caption_and_blank_rows(
    sheet: SheetGrid,
    row_range: Tuple[int, int],
    col_range: Tuple[int, int],
) -> tuple[Tuple[int, int], str | None]:
    """Strip leading full-width/single-cell title rows and blank rows so ``row_start`` lands on the actual header."""
    row_start, row_end = row_range
    col_start, col_end = col_range
    caption_parts: list[str] = []

    while row_start < row_end:
        kind, text = classify_sheet_row(sheet, row_start, col_start, col_end)
        if kind == "blank":
            row_start += 1
            continue
        if kind == "caption" and text and len(caption_parts) < 2:
            caption_parts.append(text)
            row_start += 1
            continue
        break

    caption = " / ".join(caption_parts) if caption_parts else None
    return (row_start, row_end), caption


def _parse_subtable(
    sheet: SheetGrid,
    row_range: Tuple[int, int],
    col_range: Tuple[int, int],
    header_detector: SpreadsheetHeaderDetector | None = None,
) -> dict:
    (row_start, row_end), caption = _strip_leading_caption_and_blank_rows(
        sheet, row_range, col_range
    )
    col_start, col_end = col_range
    visual = build_region_visual(
        sheet,
        Region(min_row=row_start, min_col=col_start, max_row=row_end, max_col=col_end),
    )
    spec = detect_headers(
        TableRegion(visual=visual),
        llm=header_detector,
        caption=caption,
    )
    effective_row_end = row_start + spec.data_end_row
    header_rows = list(range(row_start, row_start + spec.header_row_count))
    header_cols = list(range(col_start, col_start + spec.row_header_col_count))

    all_columns: Union[pd.Index, pd.MultiIndex, None]
    if spec.header_row_count == 0:
        data_row_start = row_start
        all_columns = None
    else:
        data_row_start = row_start + spec.header_row_count
        all_columns = _build_column_multiindex(sheet, header_rows, col_range)

    row_header_column_tuples: list[tuple[str, ...]] = []
    if all_columns is not None and spec.row_header_col_count > 0:
        for col_idx in range(min(spec.row_header_col_count, len(all_columns))):
            col_entry = all_columns[col_idx]
            if isinstance(col_entry, tuple):
                row_header_column_tuples.append(tuple(str(part) for part in col_entry))
            else:
                row_header_column_tuples.append((str(col_entry),))

    if spec.row_header_col_count == 0:
        data_col_start = col_start
        row_index = None
    elif data_row_start > effective_row_end:
        data_col_start = col_start + spec.row_header_col_count
        row_index = None
    else:
        data_col_start = col_start + spec.row_header_col_count
        row_index = _build_row_multiindex(
            sheet,
            header_cols,
            (data_row_start, effective_row_end),
        )

    data = []
    if data_row_start <= effective_row_end:
        for row in range(data_row_start, effective_row_end + 1):
            row_data = []
            for col in range(data_col_start, col_end + 1):
                row_data.append(sheet_display_value(sheet, row, col))
            data.append(row_data)

    columns = all_columns
    if columns is not None and spec.row_header_col_count:
        columns = cast(Axes, columns[spec.row_header_col_count :])

    dataframe = pd.DataFrame(data, columns=columns, index=row_index)

    excel_range = (
        f"{get_column_letter(col_start)}{row_start}:"
        f"{get_column_letter(col_end)}{effective_row_end}"
    )
    return {
        "df": dataframe,
        "header_spec": spec,
        "row_header_column_tuples": tuple(row_header_column_tuples),
        "excel_range": excel_range,
    }
