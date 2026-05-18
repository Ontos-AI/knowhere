from __future__ import annotations

import os
from dataclasses import dataclass
from typing import cast

from app.services.document_parser.html_parser import first_cols_rows_html
from app.services.document_parser.identifiers import gen_str_codes
from app.services.document_parser.inline_asset import build_table_asset_row
from app.services.document_parser.markdown_deferred_task import (
    MarkdownDeferredSummaryTask,
    TableDeferredSummaryTask,
)
from app.services.document_parser.markdown_parse_state import ParserRowValues
from app.services.document_parser.table_text_parser import sanitize_table_name_from_header

from shared.utils.chunk_refs import build_chunk_ref
from app.services.common.file_utils import path_handle


@dataclass(frozen=True)
class MarkdownTableAsset:
    content_item: str
    row_values: ParserRowValues
    deferred_task: MarkdownDeferredSummaryTask | None
    relative_path: str


@dataclass(frozen=True)
class MarkdownTableAssetRequest:
    table_html: str
    table_dir: str
    table_count: int
    timestamp: str
    current_page_number: int
    summary_table: bool
    row_index: int


def build_markdown_table_asset(
    request: MarkdownTableAssetRequest,
) -> MarkdownTableAsset:
    first_row_text, _first_col_text = first_cols_rows_html(request.table_html)
    table_index = f"table-{request.table_count}"

    raw_table_name = (
        sanitize_table_name_from_header(first_row_text) if first_row_text else ""
    )
    table_name = _sanitize_table_file_stem(
        f"table-{str(request.table_count)} {raw_table_name}"
    )
    relative_table_path = f"tables/{table_name}.html"
    table_ref = build_chunk_ref(relative_table_path)
    table_content_item = f"\n{table_ref}\n"
    table_path = os.path.join(request.table_dir, f"{table_name}.html")
    _write_table_html(table_path=table_path, table_html=request.table_html)

    table_row = build_table_asset_row(
        content=request.table_html,
        relative_path=relative_table_path,
        summary=table_index,
        keywords="",
        know_id=gen_str_codes((request.table_html + str(request.table_count))),
        addtime=request.timestamp,
        page_nums=str(request.current_page_number)
        if request.current_page_number > 0
        else "",
    )

    deferred_task = None
    if request.summary_table:
        deferred_task = TableDeferredSummaryTask(
            row_index=request.row_index,
            table_html=request.table_html,
            table_dir=request.table_dir,
            table_name=table_name,
            table_count=request.table_count - 1,
        )

    return MarkdownTableAsset(
        content_item=table_content_item,
        row_values=cast(ParserRowValues, table_row.to_list()),
        deferred_task=deferred_task,
        relative_path=relative_table_path,
    )


def _sanitize_table_file_stem(raw_name: str) -> str:
    table_name = path_handle(raw_name, mode="clean_single")
    if not isinstance(table_name, str) or not table_name:
        raise ValueError(f"Failed to sanitize Markdown table name: {raw_name}")
    return table_name


def _write_table_html(*, table_path: str, table_html: str) -> None:
    table_html_with_border = table_html.replace("<table>", "<table border='1'>").replace(
        "<table ",
        "<table border='1' ",
    )
    with open(table_path, "w", encoding="utf-8") as table_file:
        table_file.write(table_html_with_border)
