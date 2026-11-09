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
    parse_tb_contents,
    parse_tb_keywords,
    postprocess_tb,
)
from bs4 import BeautifulSoup
from loguru import logger

from shared.core.exceptions.domain_exceptions import TableParsingException
from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.services.chunks.path_segments import join_document_path
from app.services.common.file_loading import load_file_bytes
from app.services.document_parser.assets.asset_file_names import (
    ASSET_KIND_TABLE,
    build_asset_file_stem,
    build_asset_index_label,
)
from shared.utils.text_utils import tokenize2stw_remove


@dataclass(frozen=True)
class ExcelWorkbookParseRequest:
    file_path: str
    file_name: str
    output_dir: str
    baseurl: str
    base_llm_paras: dict[str, Any]
    relative_root: str | None
    use_precision_mode: bool
    include_hidden_sheets: bool


def parse_xlsx(
    file_path: str,
    file_name: str,
    output_dir: str,
    baseurl: str,
    base_llm_paras: dict[str, Any] | None = None,
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
        relative_root=relative_root,
        use_precision_mode=use_precision_mode,
        include_hidden_sheets=include_hidden_sheets,
    )
    return parse_excel_workbook(request)


def parse_excel_workbook(request: ExcelWorkbookParseRequest) -> pd.DataFrame:
    time_stamp = get_str_time()
    sheets_dict = _load_excel_sheets(request)
    parsed_rows: list[ParsedRow] = []
    table_seq = 0

    for sheet_name, sheet_frame in _iter_unique_sheets(sheets_dict):
        table_rows = _parse_excel_sheet(
            request=request,
            sheet_name=sheet_name,
            sheet_frame=sheet_frame,
            time_stamp=time_stamp,
            table_index=table_seq + 1,
        )
        if table_rows:
            table_seq += 1
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
) -> dict[str, pd.DataFrame]:
    table_data = load_file_bytes(request.file_path, file_url=request.baseurl)
    table_stream = io.BytesIO(table_data)

    os.makedirs(os.path.join(request.output_dir, "tables"), exist_ok=True)

    if not request.use_precision_mode:
        return pd.read_excel(table_stream, sheet_name=None)

    logger.info("Using precision mode for Excel header detection")
    try:
        return parse_excel_structure(
            table_stream,
            include_hidden_sheets=request.include_hidden_sheets,
        )
    except Exception as exc:
        logger.warning(f"Precision mode failed, falling back to legacy mode: {exc}")
        table_stream.seek(0)
        return pd.read_excel(table_stream, sheet_name=None)


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
    time_stamp: str,
    table_index: int,
) -> list[ParsedRow]:
    parsed_rows: list[ParsedRow] = []
    sheet_attrs = dict(sheet_frame.attrs)

    try:
        table_frame = postprocess_tb(sheet_frame, drop=True)
        if len(table_frame) == 0 or table_frame.empty or table_frame.isna().all().all():
            return parsed_rows

        sheet_attrs = dict(table_frame.attrs)
        row_header_cols = int(sheet_attrs.get("row_header_col_count") or 0)
        logical_sheet_name = _sheet_name_from_attrs(sheet_attrs, fallback=sheet_name)
        subtable_title = _subtable_title_from_attrs(sheet_attrs)
        header_row_count = sheet_attrs.get("header_row_count")
        include_header = True if header_row_count is None else header_row_count != 0

        table_html = parse_tb_contents(
            table_frame,
            row_header_cols=row_header_cols,
            include_header=include_header,
        )

        parsed_rows.append(
            _write_excel_table_asset(
                request=request,
                sheet_name=logical_sheet_name,
                subtable_title=subtable_title,
                table_frame=table_frame,
                table_html=table_html,
                time_stamp=time_stamp,
                table_index=table_index,
                extra_metadata=_excel_table_extra_metadata(
                    sheet_attrs, sheet_name=logical_sheet_name
                ),
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


def _write_excel_table_asset(
    *,
    request: ExcelWorkbookParseRequest,
    sheet_name: str,
    subtable_title: str,
    table_frame: pd.DataFrame,
    table_html: str,
    time_stamp: str,
    table_index: int,
    extra_metadata: dict[str, object] | None = None,
) -> ParsedRow:
    title, keywords, summary, entities = _summarize_excel_table(
        table_frame=table_frame,
        table_html=table_html,
        sheet_name=sheet_name,
        llm_parameters=request.base_llm_paras,
    )
    table_label = _table_label(sheet_name=sheet_name, subtable_title=subtable_title)
    table_index_label = build_asset_index_label(ASSET_KIND_TABLE, table_index)
    table_summary = (
        f"{table_index_label}\n{summary}" if summary else table_index_label
    )
    effective_name = title or table_label
    table_stem = build_asset_file_stem(
        ASSET_KIND_TABLE,
        table_index,
        remove_spaces(effective_name) if effective_name else "",
    )
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
            entities=entities,
            asset_title=title or "",
            extra_metadata=extra_metadata,
        )
    )


def _excel_table_extra_metadata(
    attrs: dict[str, Any], *, sheet_name: str
) -> dict[str, object] | None:
    if "header_row_count" not in attrs:
        return None
    column_headers = attrs.get("column_headers") or ()
    meta: dict[str, object] = {
        "header_row_count": int(attrs["header_row_count"]),
        "row_header_col_count": int(attrs.get("row_header_col_count") or 0),
        "column_headers": [str(name) for name in column_headers],
        "sheet_name": sheet_name,
        "excel_range": str(attrs.get("excel_range") or ""),
    }
    caption = str(attrs.get("caption") or "").strip()
    if caption:
        meta["caption"] = caption
    return meta


def _sheet_name_from_attrs(attrs: dict[str, Any], *, fallback: str) -> str:
    sheet_name = str(attrs.get("sheet_name") or fallback).strip()
    return sheet_name or fallback


def _subtable_title_from_attrs(attrs: dict[str, Any]) -> str:
    caption = str(attrs.get("caption") or "").strip()
    if caption:
        return caption
    try:
        subtable_count = int(attrs.get("subtable_count") or 1)
        subtable_index = int(attrs.get("subtable_index") or 1)
    except (TypeError, ValueError):
        subtable_count = 1
        subtable_index = 1
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
    return join_document_path(parts)


def _clean_path_segment(value: str) -> str:
    # Keep semantic ``/`` for join_document_path escaping; only neutralize
    # filesystem-hostile backslashes in sheet/subtable labels.
    return str(value).strip().replace("\\", "_")


def _summarize_excel_table(
    *,
    table_frame: pd.DataFrame,
    table_html: str,
    sheet_name: str,
    llm_parameters: dict[str, Any],
) -> tuple[str | None, str, str | None, str]:
    from app.services.document_parser.support.parser_rows import serialize_entities

    mechanical_keywords = parse_tb_keywords(table_frame)

    if llm_parameters["summary_table"]:
        from shared.services.ai.summary.engine import summarize

        # Tables are Contract B assets: title + summary + entities from HTML.
        result = summarize(
            mode="asset",
            text=table_html,
            max_keywords=3,
            usage_task="parser.excel.table",
        )
        return (
            result.title or None,
            result.keywords_str() or mechanical_keywords,
            result.summary or None,
            serialize_entities(result.entities),
        )

    return None, mechanical_keywords, None, ""


def _rows_to_dataframe(parsed_rows: list[ParsedRow]) -> pd.DataFrame:
    rows_builder = ParsedRowsBuilder()
    for row in parsed_rows:
        rows_builder.append(row)
    table_df = rows_builder.to_dataframe()
    return process_dup_paths_df(table_df)
