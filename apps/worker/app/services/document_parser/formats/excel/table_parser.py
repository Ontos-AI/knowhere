# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportOptionalSubscript=false, reportReturnType=false
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from typing import Any

import pandas as pd
from app.services.document_parser.tables.dataframe_helpers import process_dup_paths_df
from app.services.document_parser.formats.excel.structure_parser import parse_excel_structure
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.support.parser_rows import ParsedRow, ParsedRowsBuilder
from app.services.document_parser.support.path_helpers import remove_spaces
from app.services.document_parser.tables.table_asset_writer import (
    TableAssetInput,
    write_table_asset,
)
from app.services.document_parser.tables.table_frame_parser import (
    parse_headers,
    parse_tb_contents,
    parse_tb_keywords,
    postprocess_tb,
)
from bs4 import BeautifulSoup
from loguru import logger

from shared.core.exceptions.domain_exceptions import TableParsingException
from shared.core.exceptions.knowhere_exception import KnowhereException
from app.services.common.file_loading import load_file_bytes
from app.services.common.file_utils import path_handle
from shared.utils.text_utils import tokenize2stw_remove


@dataclass(frozen=True)
class ExcelWorkbookParseRequest:
    file_path: str
    file_name: str
    output_dir: str
    baseurl: str
    base_llm_paras: dict[str, Any]
    window_h: int
    relative_root: str | None
    use_precision_mode: bool
    include_hidden_sheets: bool


def parse_xlsx(
    file_path: str,
    file_name: str,
    output_dir: str,
    baseurl: str,
    base_llm_paras: dict[str, Any] | None = None,
    window_h: int = 10,
    relative_root: str | None = None,
    use_precision_mode: bool = True,
    include_hidden_sheets: bool = False,
) -> pd.DataFrame:
    request = ExcelWorkbookParseRequest(
        file_path=file_path,
        file_name=file_name,
        output_dir=output_dir,
        baseurl=baseurl,
        base_llm_paras=_normalise_llm_parameters(base_llm_paras),
        window_h=window_h,
        relative_root=relative_root,
        use_precision_mode=use_precision_mode,
        include_hidden_sheets=include_hidden_sheets,
    )
    return parse_excel_workbook(request)


def parse_excel_workbook(request: ExcelWorkbookParseRequest) -> pd.DataFrame:
    time_stamp = get_str_time()
    sheets_dict, precision_mode_active = _load_excel_sheets(request)
    parsed_rows: list[ParsedRow] = []

    for sheet_name, sheet_frame in _iter_unique_sheets(sheets_dict):
        table_rows = _parse_excel_sheet(
            request=request,
            sheet_name=sheet_name,
            sheet_frame=sheet_frame,
            precision_mode_active=precision_mode_active,
            time_stamp=time_stamp,
        )
        parsed_rows.extend(table_rows)

    return _rows_to_dataframe(parsed_rows)


def _normalise_llm_parameters(
    base_llm_paras: dict[str, Any] | None,
) -> dict[str, Any]:
    llm_parameters = dict(base_llm_paras or {})
    llm_parameters.setdefault("summary_table", False)
    llm_parameters.setdefault("stopwords", [])
    return llm_parameters


def _load_excel_sheets(
    request: ExcelWorkbookParseRequest,
) -> tuple[dict[str, pd.DataFrame], bool]:
    table_data = load_file_bytes(request.file_path, file_url=request.baseurl)
    table_stream = io.BytesIO(table_data)

    os.makedirs(os.path.join(request.output_dir, "tables"), exist_ok=True)

    if not request.use_precision_mode:
        return pd.read_excel(table_stream, sheet_name=None), False

    logger.info("Using precision mode for Excel header detection")
    try:
        return (
            parse_excel_structure(
                table_stream,
                include_hidden_sheets=request.include_hidden_sheets,
            ),
            True,
        )
    except Exception as exc:
        logger.warning(f"Precision mode failed, falling back to legacy mode: {exc}")
        table_stream.seek(0)
        return pd.read_excel(table_stream, sheet_name=None), False


def _iter_unique_sheets(
    sheets_dict: dict[str, pd.DataFrame],
) -> list[tuple[str, pd.DataFrame]]:
    used_sheet_names: list[str] = []
    unique_sheets: list[tuple[str, pd.DataFrame]] = []

    for raw_sheet_name, sheet_content in sheets_dict.items():
        sheet_name = raw_sheet_name.strip()
        if sheet_name in used_sheet_names:
            sheet_name = sheet_name + str(len(used_sheet_names))
        else:
            used_sheet_names.append(sheet_name)
        unique_sheets.append((sheet_name, sheet_content))

    return unique_sheets


def _parse_excel_sheet(
    *,
    request: ExcelWorkbookParseRequest,
    sheet_name: str,
    sheet_frame: pd.DataFrame,
    precision_mode_active: bool,
    time_stamp: str,
) -> list[ParsedRow]:
    parsed_rows: list[ParsedRow] = []
    sheet_attrs = dict(sheet_frame.attrs)

    try:
        table_frame = postprocess_tb(sheet_frame, drop=True)
        if len(table_frame) == 0 or table_frame.empty or table_frame.isna().all().all():
            return parsed_rows

        if not precision_mode_active:
            table_frame = parse_headers(table_frame, paras=request.base_llm_paras)

        table_frame = _drop_source_row_columns(table_frame)
        row_header_cols = int(table_frame.attrs.get("row_header_cols", 0))
        logical_sheet_name = _sheet_name_from_attrs(sheet_attrs, fallback=sheet_name)
        subtable_title = _subtable_title_from_attrs(sheet_attrs)

        _table_paths, table_html = parse_tb_contents(
            table_frame,
            parent_dic={request.file_name: {logical_sheet_name: {}}},
            file_name=request.file_name,
            sheet_name=logical_sheet_name,
            row_header_cols=row_header_cols,
        )

        parsed_rows.append(
            _write_excel_table_asset(
                request=request,
                sheet_name=logical_sheet_name,
                subtable_title=subtable_title,
                table_frame=table_frame,
                table_html=table_html,
                time_stamp=time_stamp,
            )
        )
        return parsed_rows
    except KnowhereException:
        raise
    except Exception as exc:
        logger.error(f"Table parsing failed: {exc}")
        raise TableParsingException(
            user_message="Failed to parse Excel table content",
            reason="TABLE_PROCESSING_FAILED",
            internal_message=str(exc),
            original_exception=exc,
        ) from exc


def _drop_source_row_columns(table_frame: pd.DataFrame) -> pd.DataFrame:
    source_row_columns = [
        column
        for column in table_frame.columns
        if (isinstance(column, tuple) and column[0] == "_src_row")
        or column == "_src_row"
    ]
    if not source_row_columns:
        return table_frame
    return table_frame.drop(columns=source_row_columns)


def _write_excel_table_asset(
    *,
    request: ExcelWorkbookParseRequest,
    sheet_name: str,
    subtable_title: str,
    table_frame: pd.DataFrame,
    table_html: str,
    time_stamp: str,
) -> ParsedRow:
    title, keywords, summary = _summarize_excel_table(
        table_frame=table_frame,
        table_html=table_html,
        sheet_name=sheet_name,
        llm_parameters=request.base_llm_paras,
    )
    table_label = _table_label(sheet_name=sheet_name, subtable_title=subtable_title)
    table_index = f"table-{table_label}"
    table_summary = f"{table_index}\n{summary}" if summary else table_index
    effective_name = title or table_label
    table_stem = path_handle(
        remove_spaces("table-" + effective_name),
        mode="clean_single",
    )
    if not isinstance(table_stem, str) or not table_stem:
        raise ValueError(f"Failed to sanitize Excel table name: {effective_name}")
    table_name = table_stem + ".html"
    table_html_string = BeautifulSoup(table_html, features="html.parser").prettify()
    know_id = gen_str_codes(table_html + str(table_label))
    table_tokens = tokenize2stw_remove(
        [table_summary, keywords],
        request.base_llm_paras["stopwords"],
    )

    return write_table_asset(
        TableAssetInput(
            html=table_html_string,
            output_dir=request.output_dir,
            table_name=table_name,
            summary=table_summary,
            keywords=keywords,
            know_id=know_id,
            addtime=time_stamp,
            tokens=table_tokens,
            path=_build_excel_table_path(
                request=request,
                sheet_name=sheet_name,
                subtable_title=subtable_title,
            ),
            asset_path=f"tables/{table_name}",
        )
    )


def _sheet_name_from_attrs(attrs: dict[str, Any], *, fallback: str) -> str:
    sheet_name = str(attrs.get("sheet_name") or fallback).strip()
    return sheet_name or fallback


def _subtable_title_from_attrs(attrs: dict[str, Any]) -> str:
    try:
        subtable_count = int(attrs.get("subtable_count") or 1)
        subtable_index = int(attrs.get("subtable_index") or 1)
    except (TypeError, ValueError):
        subtable_count = 1
    if subtable_count <= 1:
        return ""
    title = str(attrs.get("subtable_title") or "").strip()
    return title or f"子表 {subtable_index}"


def _table_label(*, sheet_name: str, subtable_title: str) -> str:
    return f"{sheet_name}-{subtable_title}" if subtable_title else sheet_name


def _build_excel_table_path(
    *,
    request: ExcelWorkbookParseRequest,
    sheet_name: str,
    subtable_title: str,
) -> str:
    document_root = str(request.relative_root or request.file_name).strip()
    parts = [
        _clean_path_segment(document_root),
        _clean_path_segment(sheet_name),
    ]
    if subtable_title:
        parts.append(_clean_path_segment(subtable_title))
    return "/".join(part for part in parts if part)


def _clean_path_segment(value: str) -> str:
    return str(value).strip().replace("/", "_").replace("\\", "_")


def _summarize_excel_table(
    *,
    table_frame: pd.DataFrame,
    table_html: str,
    sheet_name: str,
    llm_parameters: dict[str, Any],
) -> tuple[str | None, str, str | None]:
    mechanical_keywords = parse_tb_keywords(table_frame)

    if llm_parameters["summary_table"]:
        from app.services.document_parser.formats.text.parser import (
            extract_title_keywords_summary,
        )

        title, keywords, summary = extract_title_keywords_summary(
            table_html,
            max_keywords=3,
        )
        return title, keywords or mechanical_keywords, summary

    return None, mechanical_keywords, None


def _rows_to_dataframe(parsed_rows: list[ParsedRow]) -> pd.DataFrame:
    rows_builder = ParsedRowsBuilder()
    for row in parsed_rows:
        rows_builder.append(row)
    table_df = rows_builder.to_dataframe()
    return process_dup_paths_df(table_df)
