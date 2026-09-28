"""Persist publication token rows with PostgreSQL COPY on the owner connection."""

from __future__ import annotations

import io
import struct
import time
from collections.abc import Iterator, Sequence
from typing import Protocol, cast

from sqlalchemy.orm import Session

from shared.services.jobs.lifecycle.publication_trace_sql import record_publication_sql

# Keep the production-shaped publication in one COPY while retaining a bound
# for larger documents so token persistence does not add avoidable round trips.
_COPY_BATCH_SIZE = 100_000
_TOKEN_COLUMNS = (
    "id", "map_unit_id", "channel", "token", "token_hash", "frequency",
)
TokenCopyRow = list[str | int]
_COPY_SQL = (
    "COPY document_map_unit_tokens "
    "(id, map_unit_id, channel, token, token_hash, frequency) "
    "FROM STDIN WITH (FORMAT BINARY)"
)


class _CopyCursor(Protocol):
    def copy_expert(self, sql: str, file: io.BytesIO) -> None: ...

    def close(self) -> None: ...


class _PsycopgConnection(Protocol):
    def cursor(self) -> _CopyCursor: ...


class _AsyncpgConnection(Protocol):
    async def copy_records_to_table(
        self,
        table_name: str,
        *,
        records: list[tuple[str | int, ...]],
        columns: tuple[str, ...],
    ) -> str: ...


class _AsyncpgAdapter(Protocol):
    def run_async(self, function: object) -> object: ...


def _iter_batches(
    rows: Sequence[TokenCopyRow],
) -> Iterator[list[tuple[str | int, ...]]]:
    for start in range(0, len(rows), _COPY_BATCH_SIZE):
        yield [
            tuple(cast(str | int, value) for value in row)
            for row in rows[start : start + _COPY_BATCH_SIZE]
        ]


def _copy_psycopg(
    connection: _PsycopgConnection,
    records: list[tuple[str | int, ...]],
) -> None:
    buffer = _encode_binary_rows(records)
    buffer.seek(0)
    cursor = connection.cursor()
    try:
        cursor.copy_expert(_COPY_SQL, buffer)
    finally:
        cursor.close()


def _encode_binary_rows(records: list[tuple[str | int, ...]]) -> io.BytesIO:
    """Encode token rows for PostgreSQL's binary COPY protocol."""
    buffer = io.BytesIO()
    encoded_text_cache: dict[str, bytes] = {}
    buffer.write(b"PGCOPY\n\xff\r\n\x00")
    buffer.write(struct.pack("!ii", 0, 0))
    for record in records:
        buffer.write(struct.pack("!h", len(record)))
        for value in record:
            if isinstance(value, int):
                encoded = struct.pack("!i", value)
            else:
                encoded = encoded_text_cache.get(value)
                if encoded is None:
                    encoded = value.encode("utf-8")
                    encoded_text_cache[value] = encoded
            buffer.write(struct.pack("!i", len(encoded)))
            buffer.write(encoded)
    buffer.write(struct.pack("!h", -1))
    return buffer


def insert_token_rows_with_copy(
    db: Session,
    rows: list[dict[str, object]],
) -> None:
    """COPY token dictionaries within the publication transaction."""
    insert_token_records_with_copy(
        db,
        [
            [cast(str | int, row[column]) for column in _TOKEN_COLUMNS]
            for row in rows
        ],
    )


def insert_token_records_with_copy(
    db: Session,
    rows: Sequence[TokenCopyRow],
) -> None:
    """COPY compact token rows within the publication transaction."""
    if not rows:
        return
    connection = db.connection()
    driver = connection.dialect.driver
    raw_connection = connection.connection
    for records in _iter_batches(rows):
        started_at = time.perf_counter()
        did_succeed = False
        try:
            if driver == "psycopg2":
                _copy_psycopg(
                    cast(_PsycopgConnection, raw_connection.driver_connection),
                    records,
                )
            elif driver == "asyncpg":
                adapter = cast(_AsyncpgAdapter, raw_connection.dbapi_connection)
                adapter.run_async(
                    lambda async_connection: cast(
                        _AsyncpgConnection, async_connection
                    ).copy_records_to_table(
                        "document_map_unit_tokens",
                        records=records,
                        columns=_TOKEN_COLUMNS,
                    )
                )
            else:
                raise RuntimeError(
                    f"PostgreSQL COPY requires psycopg2 or asyncpg; got {driver}"
                )
            did_succeed = True
        finally:
            record_publication_sql(
                connection,
                duration_ms=(time.perf_counter() - started_at) * 1_000,
                is_batch=True,
                is_manual_write=True,
                write_row_count=len(records) if did_succeed else None,
            )
