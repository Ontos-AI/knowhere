from __future__ import annotations

import os
from dataclasses import dataclass

from app.services.document_parser.support.parser_rows import ParsedRow


@dataclass(frozen=True)
class TableAssetInput:
    html: str
    output_dir: str
    table_name: str
    summary: str
    keywords: str
    know_id: str
    addtime: str
    content: str | None = None
    tokens: str = ""
    length: int | None = None


def write_table_asset(table_input: TableAssetInput) -> ParsedRow:
    table_dir = os.path.join(table_input.output_dir, "tables")
    os.makedirs(table_dir, exist_ok=True)
    table_filename = _ensure_html_extension(table_input.table_name)
    table_path = os.path.join(table_dir, table_filename)
    with open(table_path, "w", encoding="utf-8") as table_file:
        table_file.write(table_input.html)
    row_content = table_input.content if table_input.content is not None else table_input.html
    return ParsedRow(
        content=row_content,
        path=f"tables/{table_filename}",
        type="table",
        keywords=table_input.keywords,
        summary=table_input.summary,
        know_id=table_input.know_id,
        tokens=table_input.tokens,
        connectto="",
        addtime=table_input.addtime,
        length=table_input.length,
    )


def _ensure_html_extension(table_name: str) -> str:
    return table_name if table_name.endswith(".html") else f"{table_name}.html"
