"""``corpus.query_table`` — read-only SELECT over one table chunk's grid."""

from __future__ import annotations

from shared.services.retrieval.corpus_revision_context import CorpusRevisionContext

from shared.services.retrieval.corpus_storage import CorpusStorage

import sqlite3
from typing import Any

from sqlalchemy import select

from shared.models.database.job_result import JobResult
from shared.services.retrieval.agent_tools.registry import (
    ToolContext,
    ToolResult,
    register_tool,
)
from shared.services.retrieval.hydration.table_grid import (
    TableDownloadError,
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
            "document_id": {
                "type": "string",
                "description": "Document owning the table chunk.",
            },
            "chunk_id": {
                "type": "string",
                "description": "The table chunk_id, from a prior read/recall/grep row.",
            },
            "sql": {
                "type": "string",
                "description": "A single SELECT. LIMIT is added when omitted.",
            },
        },
        "required": ["document_id", "chunk_id", "sql"],
        "additionalProperties": False,
    },
)
async def query_table(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    corpusStorage: CorpusStorage = CorpusStorage.resolve_namespace(ctx.namespace)
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
            select(
                corpusStorage.Document, JobResult.job_id,
                JobResult.document_metadata["result_raw_prefix"].as_string(),
            )
            .select_from(corpusStorage.Document)
            .outerjoin(JobResult, JobResult.id == CorpusRevisionContext.build_revision_column(corpusStorage.Document))
            .where(corpusStorage.Document.document_id == document_id)
            .where(corpusStorage.Document.user_id == corpusStorage.resolve_owner(ctx.user_id))
            .where(corpusStorage.Document.namespace == ctx.namespace)
            .where(corpusStorage.Document.status == "active")
            .where(ctx.document_scope.predicate(corpusStorage.Document.document_id))
        )
    ).first()
    if document is None:
        return ToolResult(text="", error=f"unknown document_id: {document_id}")
    doc, job_id, result_raw_prefix = document
    if not CorpusRevisionContext.resolve_revision(doc.document_id, doc.current_job_result_id):
        return ToolResult(text="", error=f"unknown document_id: {document_id}")

    row = (
        await ctx.db.execute(
            select(corpusStorage.DocumentChunk, corpusStorage.DocumentSection.section_path)
            .select_from(corpusStorage.DocumentChunk)
            .outerjoin(
                corpusStorage.DocumentSection,
                corpusStorage.DocumentSection.section_id == corpusStorage.DocumentChunk.section_id,
            )
            .where(corpusStorage.DocumentChunk.document_id == document_id)
            .where(corpusStorage.DocumentChunk.job_result_id == CorpusRevisionContext.resolve_revision(doc.document_id, doc.current_job_result_id))
            .where(corpusStorage.DocumentChunk.chunk_id == chunk_id)
            .where(corpusStorage.DocumentChunk.chunk_type == "table")
        )
    ).first()
    if row is None:
        return ToolResult(text="", error=f"unknown table chunk_id: {chunk_id} in {document_id}")
    chunk, section_path = row
    try:
        table_html = load_table_html(
            {
                "content": chunk.content,
                "file_path": chunk.file_path,
                "job_id": job_id,
                "result_raw_prefix": result_raw_prefix,
            }
        )
    except TableDownloadError as exc:
        return ToolResult(
            text="", error=f"table download failed for {chunk_id}: {exc}"
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
