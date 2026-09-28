"""Contracts for connection-local publication SQL observation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import cast

import pytest
from loguru import logger
from sqlalchemy import Integer, String, create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from shared.services.jobs.lifecycle.publication_trace_sql import (
    PUBLICATION_TRACE_INFO_KEY,
    attach_publication_trace,
    clear_publication_trace,
    install_publication_trace_sql_instrumentation,
    publication_sql_aggregates,
    publication_trace_connection,
    read_publication_trace,
    record_publication_sql,
    set_publication_trace_stage,
)
from shared.services.jobs.lifecycle.publication_trace import PublicationTrace


@dataclass
class RecordingTrace:
    sql_updates: list[tuple[str, int, float, float, int]] = field(default_factory=list)
    active_stage: str | None = None

    def record_sql(
        self,
        *,
        stage: str,
        statement_count: int,
        total_sql_ms: float,
        max_statement_ms: float,
        batch_count: int | None = None,
    ) -> None:
        self.sql_updates.append(
            (stage, statement_count, total_sql_ms, max_statement_ms, batch_count or 0)
        )


class Base(DeclarativeBase):
    pass


class PublishedRow(Base):
    __tablename__ = "published_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[str] = mapped_column(String(32))


def test_first_connection_on_fresh_engine_observes_sql(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'first-connection.db'}")
    install_publication_trace_sql_instrumentation(engine)
    trace = RecordingTrace()

    with engine.connect() as connection:
        with publication_trace_connection(connection, trace, stage="chunks_persist"):
            connection.execute(text("SELECT 1"))

    assert [(stage, count) for stage, count, _, _, _ in trace.sql_updates] == [
        ("chunks_persist", 1)
    ]
    engine.dispose()


def test_cursor_hooks_include_orm_flush_without_sql_payload(tmp_path) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'publication.db'}", pool_size=1, max_overflow=0
    )
    Base.metadata.create_all(engine)
    install_publication_trace_sql_instrumentation(engine)
    install_publication_trace_sql_instrumentation(engine)
    trace = RecordingTrace()

    with Session(engine) as session:
        connection = session.connection()
        with publication_trace_connection(connection, trace):
            trace.active_stage = "chunks_persist"
            session.add(PublishedRow(value="private payload"))
            session.flush()
            trace.active_stage = "serving_manifest_persist"
            session.execute(text("SELECT :secret"), {"secret": "secret value"})
            trace.active_stage = None
            aggregates = publication_sql_aggregates(connection)
            assert aggregates["chunks_persist"].statement_count == 1
            assert aggregates["serving_manifest_persist"].statement_count == 1
            assert all(aggregate.total_sql_ms >= 0 for aggregate in aggregates.values())
            assert all(
                aggregate.max_statement_ms >= 0 for aggregate in aggregates.values()
            )

        assert read_publication_trace(connection) is None
        session.rollback()

    assert [(stage, count) for stage, count, _, _, _ in trace.sql_updates] == [
        ("chunks_persist", 1),
        ("serving_manifest_persist", 1),
    ]
    assert "secret value" not in repr(trace.sql_updates)
    assert "private payload" not in repr(trace.sql_updates)
    engine.dispose()


def test_cursor_hooks_count_bulk_execute_as_one_sql_batch(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'bulk.db'}")
    Base.metadata.create_all(engine)
    install_publication_trace_sql_instrumentation(engine)
    trace = RecordingTrace()

    with Session(engine) as session:
        connection = session.connection()
        with publication_trace_connection(connection, trace, stage="tokens_persist"):
            session.execute(
                PublishedRow.__table__.insert(),
                [{"value": "first"}, {"value": "second"}],
            )
            aggregate = publication_sql_aggregates(connection)["tokens_persist"]
            assert aggregate.statement_count == 1
            assert aggregate.batch_count == 1
        session.rollback()

    assert trace.sql_updates[-1][4] == 1
    engine.dispose()


def test_pool_reuse_does_not_inherit_trace_even_if_owner_forgets_clear(
    tmp_path,
) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'reuse.db'}", pool_size=1, max_overflow=0
    )
    install_publication_trace_sql_instrumentation(engine)
    first_trace = RecordingTrace()

    with engine.connect() as first_connection:
        attach_publication_trace(first_connection, first_trace, stage="first")
        first_connection.execute(text("SELECT 1"))
        assert read_publication_trace(first_connection) is first_trace

    with engine.connect() as reused_connection:
        assert read_publication_trace(reused_connection) is None
        assert PUBLICATION_TRACE_INFO_KEY not in reused_connection.info
        reused_connection.execute(text("SELECT 2"))
        assert len(first_trace.sql_updates) == 1

        second_trace = RecordingTrace()
        attach_publication_trace(reused_connection, second_trace, stage="second")
        reused_connection.execute(text("SELECT 3"))
        assert len(second_trace.sql_updates) == 1
        assert len(first_trace.sql_updates) == 1

    engine.dispose()


def test_attach_rejects_another_trace_and_manual_operations_share_aggregates(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'adapter.db'}")
    trace = RecordingTrace()
    another_trace = RecordingTrace()

    with engine.connect() as connection:
        assert not record_publication_sql(connection, duration_ms=2.0)
        attach_publication_trace(connection, trace, stage="map_units_persist")
        set_publication_trace_stage(connection, "map_units_persist")
        with pytest.raises(RuntimeError, match="different publication trace"):
            attach_publication_trace(connection, another_trace)
        assert record_publication_sql(connection, duration_ms=2.5)
        assert record_publication_sql(
            connection, duration_ms=3.0, stage="map_units_persist"
        )
        assert trace.sql_updates[-1] == ("map_units_persist", 2, 5.5, 3.0, 0)
        assert clear_publication_trace(connection) is trace
        assert clear_publication_trace(connection) is None

    engine.dispose()


def test_failed_statement_is_counted_and_trace_is_cleared_on_exception(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'failure.db'}")
    install_publication_trace_sql_instrumentation(engine)
    trace = RecordingTrace()

    with engine.connect() as connection:
        with pytest.raises(OperationalError, match="missing_table"):
            with publication_trace_connection(
                connection, trace, stage="chunks_persist"
            ):
                connection.execute(text("SELECT * FROM missing_table"))

        assert read_publication_trace(connection) is None
        assert [(stage, count) for stage, count, _, _, _ in trace.sql_updates] == [
            ("chunks_persist", 1)
        ]

    engine.dispose()


def test_invalidated_connection_cleanup_preserves_cancellation(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'invalidated.db'}")
    install_publication_trace_sql_instrumentation(engine)
    trace = RecordingTrace()

    with engine.connect() as connection:
        with pytest.raises(asyncio.CancelledError):
            with publication_trace_connection(connection, trace):
                connection.execute(text("SELECT 1"))
                connection.invalidate()
                raise asyncio.CancelledError()

        assert connection.invalidated
        assert clear_publication_trace(connection) is None

    with engine.connect() as replacement_connection:
        assert read_publication_trace(replacement_connection) is None

    engine.dispose()


def test_failed_bulk_execute_still_counts_one_sql_batch(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'failed-bulk.db'}")
    Base.metadata.create_all(engine)
    install_publication_trace_sql_instrumentation(engine)
    trace = RecordingTrace()

    with engine.connect() as connection:
        with publication_trace_connection(connection, trace, stage="tokens_persist"):
            with pytest.raises(IntegrityError):
                connection.execute(
                    PublishedRow.__table__.insert(),
                    [
                        {"id": 1, "value": "first"},
                        {"id": 1, "value": "duplicate"},
                    ],
                )
            aggregate = publication_sql_aggregates(connection)["tokens_persist"]
            assert aggregate.statement_count == 1
            assert aggregate.batch_count == 1

    engine.dispose()


def test_repeated_publications_account_terminal_events_and_clear_pooled_trace(
    tmp_path,
) -> None:
    events: list[dict[str, object]] = []

    def capture_terminal(message: object) -> None:
        record = message.record  # type: ignore[attr-defined]
        if record["extra"].get("event") == "publication.trace.terminal":
            events.append(cast(dict[str, object], record["extra"]["publication_trace"]))

    sink_id = logger.add(capture_terminal, level="INFO")
    engine = create_engine(f"sqlite:///{tmp_path / 'real-trace.db'}")
    Base.metadata.create_all(engine)
    install_publication_trace_sql_instrumentation(engine)
    payloads: list[dict[str, object]] = []
    attempt_refs = [f"attempt_{index}" for index in range(12)]

    try:
        for index, attempt_ref in enumerate(attempt_refs):
            trace = PublicationTrace.start(
                attempt_ref=attempt_ref,
                owner="sync",
                job_id=f"job_{index}",
                job_result_id=f"result_{index}",
            )
            with Session(engine) as session:
                connection = session.connection()
                assert PUBLICATION_TRACE_INFO_KEY not in connection.info
                with publication_trace_connection(connection, trace):
                    with trace.stage("chunks_persist"):
                        session.add(PublishedRow(value="private payload"))
                        session.flush()
                    if index % 3 == 0:
                        session.rollback()
                    else:
                        session.commit()
            payloads.append(
                cast(
                    dict[str, object],
                    trace.finish(outcome="rollback" if index % 3 == 0 else "success"),
                )
            )
    finally:
        logger.remove(sink_id)
        engine.dispose()

    assert len(events) == len(attempt_refs)
    assert events == payloads
    assert [payload["attempt_ref"] for payload in events] == attempt_refs
    for payload in events:
        assert cast(dict[str, object], payload["sql"])["statement_count"] == 1
        stages = cast(dict[str, dict[str, int | float]], payload["stages"])
        assert stages["chunks_persist"]["statement_count"] == 1
        assert "private payload" not in repr(payload)
