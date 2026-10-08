"""Persist document chunks with PostgreSQL COPY for candidate publications."""

from __future__ import annotations

import io
import json
import time
from datetime import datetime, timezone
from typing import Any, Protocol, cast

from psycopg2.extensions import cursor as PsycopgCursor
from psycopg2.extensions import get_wait_callback
from psycopg2.extras import execute_values
from sqlalchemy import insert
from sqlalchemy.orm import Session
from shared.models.database.demo_corpus import DemoDocumentChunk

from shared.models.database.document import DocumentChunk
from shared.services.jobs.lifecycle.publication_trace_sql import record_publication_sql

_COPY_BATCH_SIZE: int = 1_000
_COLUMNS: tuple[str, ...] = (
    "id", "chunk_id", "user_id", "namespace", "document_id", "job_result_id",
    "section_id", "chunk_type", "content", "content_lexical_text", "path_lexical_text",
    "content_search_text", "path_search_text", "term_search_text", "source_chunk_path",
    "file_path", "chunk_metadata", "sort_order", "created_at",
)
_COPY_SQL: str = (
    "COPY document_chunks (" + ", ".join(_COLUMNS) + ") "
    r"FROM STDIN WITH (FORMAT CSV, NULL '\N')"
)

_CopyValue = str | int | None
_AsyncCopyValue = str | int | datetime | None


class _CopyCursor(Protocol):
    def copy_expert(self, sql: str, file: io.StringIO) -> None: ...

    def close(self) -> None: ...


class _PsycopgConnection(Protocol):
    def cursor(self) -> _CopyCursor: ...


class _AsyncpgConnection(Protocol):
    async def copy_records_to_table(
        self,
        table_name: str,
        *,
        records: list[tuple[_AsyncCopyValue, ...]],
        columns: tuple[str, ...],
    ) -> str: ...


class _AsyncpgAdapter(Protocol):
    def run_async(self, function: object) -> object: ...


def _serialize(value: Any, *, column: str) -> str | int | None:
    """Serialize nullable text, JSON, and timestamp values for CSV COPY."""
    if value is None and column == "created_at":
        return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    if value is None:
        return None
    if column == "chunk_metadata":
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return value


def _serialize_asyncpg(value: Any, *, column: str) -> _AsyncCopyValue:
    """Keep native asyncpg values while encoding JSON explicitly."""
    if value is None and column == "created_at":
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if column == "chunk_metadata" and value is not None:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def _encode_csv_field(value: _CopyValue) -> str:
    """Encode one CSV field while keeping empty strings distinct from NULL."""
    if value is None:
        return r"\N"
    text_value = str(value)
    if text_value == r"\N" or any(
        character in text_value for character in ',"\r\n'
    ):
        return '"' + text_value.replace('"', '""') + '"'
    return text_value


def _encode_csv_rows(records: list[tuple[_CopyValue, ...]]) -> io.StringIO:
    """Build COPY CSV with explicit NULL markers and quoted values."""
    buffer = io.StringIO()
    for record in records:
        buffer.write(",".join(_encode_csv_field(value) for value in record))
        buffer.write("\n")
    buffer.seek(0)
    return buffer


def _copy_psycopg(
    connection: _PsycopgConnection,
    records: list[tuple[_CopyValue, ...]],
) -> None:
    cursor = connection.cursor()
    try:
        if get_wait_callback() is not None:
            # psycogreen uses a process-wide callback that COPY cannot support.
            # Keep cooperative I/O and the caller's transaction intact.
            execute_values(
                cast(PsycopgCursor, cursor),
                "INSERT INTO document_chunks (" + ", ".join(_COLUMNS) + ") VALUES %s",
                records,
                page_size=_COPY_BATCH_SIZE,
            )
        else:
            buffer = _encode_csv_rows(records)
            buffer.seek(0)
            cursor.copy_expert(_COPY_SQL, buffer)
    finally:
        cursor.close()


def _insert_chunk_batch(db: Session, chunks: list[DocumentChunk | DemoDocumentChunk]) -> None:
    """Persist one bounded statement and record its actual SQL duration."""
    if chunks and isinstance(chunks[0], DemoDocumentChunk):
        demoRecords = [{column: (getattr(chunk, column) if column != "created_at" else getattr(chunk, column) or datetime.now(timezone.utc).replace(tzinfo=None)) for column in _COLUMNS} for chunk in chunks]
        db.execute(insert(DemoDocumentChunk.__table__), demoRecords)
        return
    connection = db.connection()
    raw_connection = connection.connection
    driver = connection.dialect.driver
    started_at = time.perf_counter()
    did_succeed = False
    try:
        if driver == "psycopg2":
            records: list[tuple[_CopyValue, ...]] = [
                tuple(
                    _serialize(getattr(chunk, column), column=column)
                    for column in _COLUMNS
                )
                for chunk in chunks
            ]
            _copy_psycopg(
                cast(_PsycopgConnection, raw_connection.driver_connection),
                records,
            )
        elif driver == "asyncpg":
            async_records: list[tuple[_AsyncCopyValue, ...]] = [
                tuple(
                    _serialize_asyncpg(getattr(chunk, column), column=column)
                    for column in _COLUMNS
                )
                for chunk in chunks
            ]
            adapter = cast(_AsyncpgAdapter, raw_connection.dbapi_connection)
            adapter.run_async(
                lambda async_connection: cast(
                    _AsyncpgConnection, async_connection
                ).copy_records_to_table(
                    "document_chunks",
                    records=async_records,
                    columns=_COLUMNS,
                )
            )
        else:
            raise RuntimeError(
                f"document chunk COPY requires psycopg2 or asyncpg; got {driver}"
            )
        did_succeed = True
    finally:
        record_publication_sql(
            connection,
            duration_ms=(time.perf_counter() - started_at) * 1_000,
            is_batch=True,
            is_manual_write=True,
            write_row_count=len(chunks) if did_succeed else None,
        )


def insert_chunks_with_copy(db: Session, chunks: list[DocumentChunk | DemoDocumentChunk]) -> None:
    """Persist prepared chunks in bounded batches inside the caller's transaction."""
    for start in range(0, len(chunks), _COPY_BATCH_SIZE):
        _insert_chunk_batch(db, chunks[start : start + _COPY_BATCH_SIZE])
