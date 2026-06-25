from __future__ import annotations

import os
from dataclasses import dataclass

from app.services.document_parser.support.parser_rows import ParsedRow
from shared.utils.chunk_refs import build_chunk_ref


@dataclass(frozen=True)
class TableAssetInput:
    html: str
    output_dir: str
    table_name: str
    summary: str
    keywords: str
    know_id: str
    addtime: str
    tokens: str = ""
    path: str | None = None
    asset_path: str | None = None
    entities: str = ""
    asset_title: str = ""


def write_table_asset(table_input: TableAssetInput) -> ParsedRow:
    table_dir = os.path.join(table_input.output_dir, "tables")
    os.makedirs(table_dir, exist_ok=True)
    table_filename = _ensure_html_extension(table_input.table_name)
    table_path = os.path.join(table_dir, table_filename)
    with open(table_path, "w", encoding="utf-8") as table_file:
        table_file.write(table_input.html)
    asset_ref = table_input.asset_path or f"tables/{table_filename}"
    row_content = asset_ref
    row_type = "table"
    if table_input.asset_path:
        row_type = f"table\n{build_chunk_ref(table_input.asset_path)}"
    return ParsedRow(
        content=row_content,
        path=table_input.path or f"tables/{table_filename}",
        type=row_type,
        keywords=table_input.keywords,
        summary=table_input.summary,
        know_id=table_input.know_id,
        tokens=table_input.tokens,
        connectto="",
        addtime=table_input.addtime,
        length=len(row_content),
        entities=table_input.entities,
        asset_title=table_input.asset_title,
    )


def _ensure_html_extension(table_name: str) -> str:
    return table_name if table_name.endswith(".html") else f"{table_name}.html"
