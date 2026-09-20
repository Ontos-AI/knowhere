"""``corpus.query_table`` — read-only SELECT over one table chunk's grid."""

from __future__ import annotations

import sqlite3
from typing import Any

from sqlalchemy import select

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    register_tool,
)
from shared.services.retrieval.hydration.table_grid import (
    grid_sql_rows,
    grid_to_html,
    html_to_grid,
    load_table_html,
    normalize_select_sql,
    sql_columns,
)
from shared.services.retrieval.settings import QUERY_TABLE_NAME


@register_tool(
    name="corpus.query_table",
    description=(
        "Run one read-only SELECT against a table chunk's grid. "
        "Use after read says the table is too large. Table name is t; "
        "columns are row_header plus the table's column headers. "
        "Requires document_id and chunk_id."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "document_id": {"type": "string"},
            "chunk_id": {"type": "string"},
            "sql": {
                "type": "string",
                "description": "A single SELECT. LIMIT is added when omitted.",
            },
        },
        "required": ["document_id", "chunk_id", "sql"],
    },
)
async def query_table(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    document_id = str(args.get("document_id") or "").strip()
    chunk_id = str(args.get("chunk_id") or "").strip()
    sql = normalize_select_sql(str(args.get("sql") or ""))
    if not document_id or not chunk_id:
        return ToolResult(text="", error="query_table requires document_id and chunk_id")
    if sql is None:
        return ToolResult(
            text="",
            error="query_table accepts one SELECT only (no writes or multiple statements)",
        )

    document = (
        await ctx.db.execute(
            select(Document, JobResult.job_id)
            .select_from(Document)
            .outerjoin(JobResult, JobResult.id == Document.current_job_result_id)
            .where(Document.document_id == document_id)
            .where(Document.user_id == ctx.user_id)
            .where(Document.namespace == ctx.namespace)
            .where(Document.status == "active")
            .where(ctx.document_scope.predicate(Document.document_id))
        )
    ).first()
    if document is None:
        return ToolResult(text="", error=f"unknown document_id: {document_id}")
    doc, job_id = document
    if not doc.current_job_result_id:
        return ToolResult(text="", error=f"unknown document_id: {document_id}")

    row = (
        await ctx.db.execute(
            select(DocumentChunk, DocumentSection.section_path)
            .select_from(DocumentChunk)
            .outerjoin(
                DocumentSection,
                DocumentSection.section_id == DocumentChunk.section_id,
            )
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == doc.current_job_result_id)
            .where(DocumentChunk.chunk_id == chunk_id)
            .where(DocumentChunk.chunk_type == "table")
        )
    ).first()
    if row is None:
        return ToolResult(text="", error=f"unknown table chunk_id: {chunk_id} in {document_id}")
    chunk, section_path = row
    table_html = load_table_html(
        {
            "content": chunk.content,
            "file_path": chunk.file_path,
            "job_id": job_id,
        }
    )
    grid = html_to_grid(table_html)
    if not grid:
        return ToolResult(text="", error=f"table HTML not available for {chunk_id}")

    columns = sql_columns(grid)
    data_rows = grid_sql_rows(grid)
    quoted = ", ".join(f'"{name}" TEXT' for name in columns)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(f'CREATE TABLE {QUERY_TABLE_NAME} ({quoted})')
        placeholders = ", ".join("?" for _ in columns)
        insert_sql = f'INSERT INTO {QUERY_TABLE_NAME} VALUES ({placeholders})'
        for values in data_rows:
            padded = list(values) + [""] * (len(columns) - len(values))
            connection.execute(insert_sql, padded[: len(columns)])
        cursor = connection.execute(sql)
        result_headers = [str(item[0]) for item in cursor.description or []]
        result_rows = [[str(cell) if cell is not None else "" for cell in row] for row in cursor.fetchall()]
    except sqlite3.Error as exc:
        return ToolResult(text="", error=f"query_table SQL failed: {exc}")
    finally:
        connection.close()

    result_grid = [result_headers, *result_rows] if result_headers else []
    text = (
        f"### {doc.source_file_name} ({document_id}) / {section_path} [table]\n"
        f"columns={', '.join(columns)}\n"
        f"{grid_to_html(result_grid)}"
    )
    return ToolResult(
        text=text,
        payload={
            "document_id": document_id,
            "chunk_id": chunk_id,
            "columns": columns,
            "headers": result_headers,
            "rows": result_rows,
        },
        refs=[{"document_id": document_id, "chunk_id": chunk_id}],
    )
