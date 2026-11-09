# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportOptionalSubscript=false, reportReturnType=false
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import pandas as pd
from app.services.document_parser.tables.dataframe_helpers import process_dup_paths_df
from app.services.document_parser.formats.excel.satellite_merge import TableFormulaLink
from app.services.document_parser.formats.excel.structure_parser import (
    ExcelStructureResult,
    ParsedExcelTable,
    parse_excel_structure,
)
from app.services.document_parser.formats.excel.workbook_grid import load_workbook_grid
from app.services.document_parser.support.identifiers import gen_str_codes, get_str_time
from app.services.document_parser.support.parser_rows import ParsedRow, ParsedRowsBuilder
from app.services.document_parser.support.path_helpers import remove_spaces
from app.services.document_parser.tables.table_asset_writer import (
    TableAssetInput,
    write_table_asset,
)
from bs4 import BeautifulSoup
from loguru import logger

from shared.core.exceptions.domain_exceptions import TableParsingException
from shared.core.exceptions.knowhere_exception import KnowhereException
from shared.services.chunks.chunk_connections import (
    RELATION_DERIVED_FROM,
    RELATION_DERIVES,
)
from shared.services.chunks.path_segments import join_document_path
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
    base_llm_paras: dict[str, Any]
    relative_root: str | None
    include_hidden_sheets: bool


def parse_xlsx(
    file_path: str,
    file_name: str,
    output_dir: str,
    base_llm_paras: dict[str, Any] | None = None,
    relative_root: str | None = None,
    include_hidden_sheets: bool = False,
) -> pd.DataFrame:
    request = ExcelWorkbookParseRequest(
        file_path=file_path,
        file_name=file_name,
        output_dir=output_dir,
        base_llm_paras=_normalise_llm_parameters(base_llm_paras),
        relative_root=relative_root,
        include_hidden_sheets=include_hidden_sheets,
    )
    return parse_excel_workbook(request)


def parse_excel_workbook(request: ExcelWorkbookParseRequest) -> pd.DataFrame:
    time_stamp = get_str_time()
    os.makedirs(os.path.join(request.output_dir, "tables"), exist_ok=True)
    logger.info("Parsing spreadsheet grid, Luna regions, and formula satellites")
    workbook = load_workbook_grid(request.file_path)
    structure = parse_excel_structure(
        workbook,
        include_hidden_sheets=request.include_hidden_sheets,
    )
    return _rows_to_dataframe(_emit_tables(request, structure, time_stamp))


def _normalise_llm_parameters(
    base_llm_paras: dict[str, Any] | None,
) -> dict[str, Any]:
    llm_parameters = dict(base_llm_paras or {})
    llm_parameters.setdefault("summary_table", False)
    llm_parameters.setdefault("stopwords", [])
    return llm_parameters


def _emit_tables(
    request: ExcelWorkbookParseRequest,
    structure: ExcelStructureResult,
    time_stamp: str,
) -> list[ParsedRow]:
    know_ids = {
        table.table_key: gen_str_codes(
            table.html + str(_table_label(sheet_name=table.sheet_name, subtable_title=table.subtable_title))
        )
        for table in structure.tables
    }
    parsed_rows: list[ParsedRow] = []
    table_seq = 0
    for table in structure.tables:
        try:
            parsed_rows.append(
                _write_excel_table_asset(
                    request=request,
                    table=table,
                    time_stamp=time_stamp,
                    table_index=table_seq + 1,
                    know_id=know_ids[table.table_key],
                    connectto=_connectto_json(
                        table.table_key, structure.links, know_ids
                    ),
                )
            )
            table_seq += 1
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
    return parsed_rows


def _connectto_json(
    table_id: str,
    links: tuple[TableFormulaLink, ...],
    know_ids: dict[str, str],
) -> str:
    connections: list[dict[str, str]] = []
    for link in links:
        if link.source_key == table_id:
            target = know_ids.get(link.target_key)
            if target:
                connections.append(
                    {
                        "target": target,
                        "relation": RELATION_DERIVED_FROM,
                        "ref": link.formula,
                    }
                )
        if link.target_key == table_id:
            source = know_ids.get(link.source_key)
            if source:
                connections.append(
                    {
                        "target": source,
                        "relation": RELATION_DERIVES,
                        "ref": link.formula,
                    }
                )
    if not connections:
        return ""
    return json.dumps(connections, ensure_ascii=False)


def _write_excel_table_asset(
    *,
    request: ExcelWorkbookParseRequest,
    table: ParsedExcelTable,
    time_stamp: str,
    table_index: int,
    know_id: str,
    connectto: str,
) -> ParsedRow:
    mechanical_keywords = _keywords_from_column_headers(table.column_headers)
    if table.satellites:
        extra = ";".join(item for item in table.satellites if item.strip())
        mechanical_keywords = ";".join(
            part for part in (mechanical_keywords, extra) if part
        )
    title, keywords, summary, entities = _summarize_excel_table(
        table_html=table.html,
        mechanical_keywords=mechanical_keywords,
        llm_parameters=request.base_llm_paras,
    )
    table_label = _table_label(
        sheet_name=table.sheet_name, subtable_title=table.subtable_title
    )
    table_index_label = build_asset_index_label(ASSET_KIND_TABLE, table_index)
    table_summary = (
        f"{table_index_label}\n{summary}" if summary else table_index_label
    )
    if table.satellites:
        table_summary = f"{table_summary}\n" + " ".join(table.satellites)
    effective_name = title or table_label
    table_stem = build_asset_file_stem(
        ASSET_KIND_TABLE,
        table_index,
        remove_spaces(effective_name) if effective_name else "",
    )
    table_name = table_stem + ".html"
    table_html_string = BeautifulSoup(table.html, features="html.parser").prettify()
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
                sheet_name=table.sheet_name,
                subtable_title=table.subtable_title,
            ),
            asset_path=f"tables/{table_name}",
            entities=entities,
            asset_title=title or "",
            extra_metadata=table.extra_metadata(),
            connectto=connectto,
        )
    )


def _keywords_from_column_headers(column_headers: tuple[str, ...]) -> str:
    keywords: list[str] = []
    seen: set[str] = set()
    for header in column_headers:
        text = header.strip()
        if not text or text == "nan" or "Unnamed" in text:
            continue
        if text in seen:
            continue
        seen.add(text)
        keywords.append(text)
    return ";".join(keywords)


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
    table_html: str,
    mechanical_keywords: str,
    llm_parameters: dict[str, Any],
) -> tuple[str | None, str, str | None, str]:
    from app.services.document_parser.support.parser_rows import serialize_entities

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
