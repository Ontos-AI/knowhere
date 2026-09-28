"""Connection-local SQL observation for publication telemetry.

The publication transaction owner attaches its trace to the SQLAlchemy
connection it owns.  SQLAlchemy cursor hooks then aggregate statement timing
without retaining SQL text or bind parameters.  This module deliberately has
no logging or database side effects; the owner decides how a trace is
completed and emitted.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterator, Protocol, cast
from weakref import WeakKeyDictionary

from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import ResourceClosedError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

PUBLICATION_TRACE_INFO_KEY = "knowhere.publication_trace"
PUBLICATION_MANUAL_SQL_OBSERVER_INFO_KEY = "knowhere.publication_manual_sql_observer"
_STATEMENT_STARTED_AT_KEY = "_knowhere_publication_statement_started_at_ns"
_LISTENER_LOCK = threading.RLock()
_INSTALLED_ENGINES: WeakKeyDictionary[Engine, bool] = WeakKeyDictionary()


class PublicationSqlTrace(Protocol):
    """Minimal trace callback supported by the SQL observation seam."""

    def record_sql(
        self,
        *,
        stage: str,
        statement_count: int,
        total_sql_ms: float,
        max_statement_ms: float,
        batch_count: int | None = None,
    ) -> None:
        """Receive an aggregated SQL observation."""

    # A trace may also expose ``active_stage``.  The hook reads that optional
    # property when nested publication modules enter their explicit stage.


class PublicationManualSqlObserver(Protocol):
    """Observer for writes issued outside SQLAlchemy cursor hooks."""

    def record_manual_write(self, *, write_row_count: int | None) -> None:
        """Record one adapter-managed write and its optional row count."""


@dataclass
class SqlStatementAggregate:
    """Timing counters for one publication stage."""

    statement_count: int = 0
    batch_count: int = 0
    total_sql_ms: float = 0.0
    max_statement_ms: float = 0.0

    def record(self, duration_ms: float, *, is_batch: bool = False) -> None:
        safe_duration_ms = max(0.0, float(duration_ms))
        self.statement_count += 1
        if is_batch:
            self.batch_count += 1
        self.total_sql_ms += safe_duration_ms
        self.max_statement_ms = max(self.max_statement_ms, safe_duration_ms)


@dataclass
class PublicationConnectionTraceMetadata:
    """Mutable metadata stored in SQLAlchemy's connection-local ``info``."""

    trace: PublicationSqlTrace
    current_stage: str = "unattributed"
    aggregates: dict[str, SqlStatementAggregate] = field(default_factory=dict)

    def record(
        self, stage: str, duration_ms: float, *, is_batch: bool = False
    ) -> SqlStatementAggregate:
        normalized_stage = stage.strip() or "unattributed"
        aggregate = self.aggregates.setdefault(
            normalized_stage,
            SqlStatementAggregate(),
        )
        aggregate.record(duration_ms, is_batch=is_batch)
        return aggregate


def _sync_connection(connection: Connection | AsyncConnection) -> Connection:
    if isinstance(connection, AsyncConnection):
        sync_connection = connection.sync_connection
        if sync_connection is None:
            raise RuntimeError("async connection has not been started")
        return sync_connection
    return connection


def _connection_info(connection: object) -> dict[str, object] | None:
    if isinstance(connection, dict):
        return cast(dict[str, object], connection)
    if isinstance(connection, Connection) and connection.invalidated:
        # Accessing Connection.info would attempt to reconnect. An invalidated
        # transaction instead raises PendingRollbackError during cleanup.
        return None
    try:
        info = getattr(connection, "info", None)
    except ResourceClosedError:
        return None
    if isinstance(info, dict):
        return cast(dict[str, object], info)
    return None


def _metadata_from_connection(
    connection: Connection | AsyncConnection | object,
) -> PublicationConnectionTraceMetadata | None:
    info = _connection_info(connection)
    if info is None:
        return None
    metadata = info.get(PUBLICATION_TRACE_INFO_KEY)
    if isinstance(metadata, PublicationConnectionTraceMetadata):
        return metadata
    return None


def attach_publication_trace(
    connection: Connection | AsyncConnection,
    trace: PublicationSqlTrace,
    *,
    stage: str = "unattributed",
) -> PublicationConnectionTraceMetadata:
    """Attach ``trace`` to a SQLAlchemy connection.

    Re-attaching the same trace is idempotent and updates its current stage.
    Attaching a different trace to an uncleared connection raises, preventing
    one transaction from silently taking over another transaction's metadata.
    """

    sync_connection = _sync_connection(connection)
    info = _connection_info(sync_connection)
    if info is None:
        raise TypeError("connection does not expose SQLAlchemy connection info")

    existing = _metadata_from_connection(sync_connection)
    if existing is not None and existing.trace is not trace:
        raise RuntimeError("a different publication trace is already attached")
    if existing is None:
        existing = PublicationConnectionTraceMetadata(trace=trace)
        info[PUBLICATION_TRACE_INFO_KEY] = existing
    set_publication_trace_stage(sync_connection, stage)
    return existing


def read_publication_trace(
    connection: Connection | AsyncConnection,
) -> PublicationSqlTrace | None:
    """Read the trace attached to a SQLAlchemy connection, if any."""

    metadata = _metadata_from_connection(_sync_connection(connection))
    return metadata.trace if metadata is not None else None


def read_publication_trace_metadata(
    connection: Connection | AsyncConnection | object,
) -> PublicationConnectionTraceMetadata | None:
    """Read connection-local publication metadata without mutating it."""

    if isinstance(connection, AsyncConnection):
        connection = _sync_connection(connection)
    return _metadata_from_connection(connection)


def set_publication_trace_stage(
    connection: Connection | AsyncConnection,
    stage: str,
) -> None:
    """Set the stage used by SQL statements without retaining SQL payloads."""

    metadata = _metadata_from_connection(_sync_connection(connection))
    if metadata is None:
        raise RuntimeError("no publication trace is attached to this connection")
    metadata.current_stage = stage.strip() or "unattributed"


def attach_publication_manual_sql_observer(
    connection: Connection | AsyncConnection,
    observer: PublicationManualSqlObserver,
) -> None:
    """Attach the observer used by COPY adapters on an owner connection."""

    info = _connection_info(_sync_connection(connection))
    if info is None:
        raise TypeError("connection does not expose SQLAlchemy connection info")
    existing = info.get(PUBLICATION_MANUAL_SQL_OBSERVER_INFO_KEY)
    if existing is not None and existing is not observer:
        raise RuntimeError("a different manual SQL observer is already attached")
    info[PUBLICATION_MANUAL_SQL_OBSERVER_INFO_KEY] = observer


def clear_publication_manual_sql_observer(
    connection: Connection | AsyncConnection | object,
) -> PublicationManualSqlObserver | None:
    """Clear and return a connection's manual SQL observer."""

    if isinstance(connection, AsyncConnection):
        connection = _sync_connection(connection)
    info = _connection_info(connection)
    if info is None:
        return None
    observer = info.pop(PUBLICATION_MANUAL_SQL_OBSERVER_INFO_KEY, None)
    if observer is None:
        return None
    return cast(PublicationManualSqlObserver, observer)


def clear_publication_trace(
    connection: Connection | AsyncConnection | object,
) -> PublicationSqlTrace | None:
    """Clear and return a connection's trace metadata.

    This function is safe to call from a ``finally`` block and from pool
    ``checkin`` hooks.  It also removes an in-flight cursor timer.
    """

    if isinstance(connection, AsyncConnection):
        connection = _sync_connection(connection)
    if hasattr(connection, _STATEMENT_STARTED_AT_KEY):
        delattr(connection, _STATEMENT_STARTED_AT_KEY)
    info = _connection_info(connection)
    if info is None:
        return None
    metadata = info.pop(PUBLICATION_TRACE_INFO_KEY, None)
    if isinstance(metadata, PublicationConnectionTraceMetadata):
        return metadata.trace
    return None


@contextmanager
def publication_trace_connection(
    connection: Connection | AsyncConnection,
    trace: PublicationSqlTrace,
    *,
    stage: str = "unattributed",
) -> Iterator[PublicationConnectionTraceMetadata]:
    """Attach a trace for a bounded scope and always clear it on exit."""

    metadata = attach_publication_trace(connection, trace, stage=stage)
    try:
        yield metadata
    finally:
        clear_publication_trace(_sync_connection(connection))


def record_publication_sql(
    connection: Connection | AsyncConnection,
    *,
    duration_ms: float,
    stage: str | None = None,
    is_batch: bool = False,
    is_manual_write: bool = False,
    write_row_count: int | None = None,
) -> bool:
    """Record a database operation through the same trace interface.

    Adapters that bypass SQLAlchemy cursor events can call this function.  The
    active trace stage takes precedence over the connection's fallback stage.
    It returns ``False`` when no trace is attached.
    """

    sync_connection = _sync_connection(connection)
    if is_manual_write:
        info = _connection_info(sync_connection)
        observer = (
            info.get(PUBLICATION_MANUAL_SQL_OBSERVER_INFO_KEY)
            if info is not None
            else None
        )
        record_manual_write = getattr(observer, "record_manual_write", None)
        if callable(record_manual_write):
            record_manual_write(write_row_count=write_row_count)
    metadata = _metadata_from_connection(sync_connection)
    if metadata is None:
        return False
    effective_stage = stage if stage is not None else _active_stage(metadata)
    aggregate = metadata.record(effective_stage, duration_ms, is_batch=is_batch)
    _notify_trace(metadata.trace, effective_stage, aggregate)
    return True


def publication_sql_aggregates(
    connection: Connection | AsyncConnection,
) -> Mapping[str, SqlStatementAggregate]:
    """Return a read-only view of current per-stage SQL aggregates."""

    metadata = _metadata_from_connection(_sync_connection(connection))
    if metadata is None:
        return MappingProxyType({})
    return MappingProxyType(metadata.aggregates)


def install_publication_trace_sql_instrumentation(
    engine: Engine | AsyncEngine,
) -> None:
    """Install cursor and pool hooks once for an engine.

    Hooks only inspect connection-local metadata created by
    :func:`attach_publication_trace`; untraced SQL has no additional work
    beyond a dictionary lookup.  Pool checkin always clears metadata, which
    prevents trace state from surviving connection reuse.
    """

    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    with _LISTENER_LOCK:
        if _INSTALLED_ENGINES.get(sync_engine):
            return
        event.listen(sync_engine, "before_cursor_execute", _before_cursor_execute)
        event.listen(sync_engine, "after_cursor_execute", _after_cursor_execute)
        event.listen(sync_engine, "handle_error", _handle_error)
        event.listen(sync_engine.pool, "checkin", _on_pool_checkin)
        _INSTALLED_ENGINES[sync_engine] = True


def _before_cursor_execute(connection: Connection, *args: object) -> None:
    if _metadata_from_connection(connection) is None:
        return
    setattr(connection, _STATEMENT_STARTED_AT_KEY, time.perf_counter_ns())


def _after_cursor_execute(connection: Connection, *args: object) -> None:
    started_at = getattr(connection, _STATEMENT_STARTED_AT_KEY, None)
    if isinstance(started_at, int):
        delattr(connection, _STATEMENT_STARTED_AT_KEY)
        record_publication_sql(
            connection,
            duration_ms=(time.perf_counter_ns() - started_at) / 1_000_000,
            is_batch=bool(args[-1]) if args else False,
        )


def _handle_error(exception_context: object) -> None:
    connection = getattr(exception_context, "connection", None)
    if not isinstance(connection, Connection):
        return
    execution_context = getattr(exception_context, "execution_context", None)
    started_at = getattr(connection, _STATEMENT_STARTED_AT_KEY, None)
    if isinstance(started_at, int):
        delattr(connection, _STATEMENT_STARTED_AT_KEY)
        record_publication_sql(
            connection,
            duration_ms=(time.perf_counter_ns() - started_at) / 1_000_000,
            is_batch=bool(getattr(execution_context, "executemany", False)),
        )


def _on_pool_checkin(dbapi_connection: object, connection_record: object) -> None:
    del dbapi_connection
    clear_publication_trace(connection_record)
    clear_publication_manual_sql_observer(connection_record)


def _notify_trace(
    trace: PublicationSqlTrace,
    stage: str,
    aggregate: SqlStatementAggregate,
) -> None:
    trace.record_sql(
        stage=stage,
        statement_count=aggregate.statement_count,
        total_sql_ms=aggregate.total_sql_ms,
        max_statement_ms=aggregate.max_statement_ms,
        batch_count=aggregate.batch_count,
    )


def _active_stage(metadata: PublicationConnectionTraceMetadata) -> str:
    """Prefer the explicit trace's innermost stage during nested publication work."""

    active_stage = getattr(metadata.trace, "active_stage", None)
    if isinstance(active_stage, str) and active_stage:
        return active_stage
    return metadata.current_stage


__all__ = [
    "PUBLICATION_MANUAL_SQL_OBSERVER_INFO_KEY",
    "PUBLICATION_TRACE_INFO_KEY",
    "PublicationConnectionTraceMetadata",
    "PublicationManualSqlObserver",
    "PublicationSqlTrace",
    "SqlStatementAggregate",
    "attach_publication_trace",
    "attach_publication_manual_sql_observer",
    "clear_publication_trace",
    "clear_publication_manual_sql_observer",
    "install_publication_trace_sql_instrumentation",
    "publication_sql_aggregates",
    "publication_trace_connection",
    "read_publication_trace",
    "read_publication_trace_metadata",
    "record_publication_sql",
    "set_publication_trace_stage",
]
