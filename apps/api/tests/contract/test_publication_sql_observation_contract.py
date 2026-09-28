"""Contract for symmetric benchmark SQL observation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.support.publication_benchmark_support import ensure_benchmark_import_path

ensure_benchmark_import_path()

from scripts.publication_benchmark.publication_execution import (  # noqa: E402
    BenchmarkSqlObservation,
)
from shared.services.jobs.lifecycle.publication_trace_sql import (  # noqa: E402
    attach_publication_manual_sql_observer,
    clear_publication_manual_sql_observer,
    record_publication_sql,
)


def test_observer_counts_only_publication_sql_on_first_engine_connection(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'observation.db'}")
    observer = BenchmarkSqlObservation(engine)

    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        )
        connection.execute(text("INSERT INTO sample (id, value) VALUES (1, 'fixture')"))

    with engine.begin() as connection:
        observer.start()
        connection.execute(text("SELECT value FROM sample WHERE id = 1"))
        connection.execute(text("INSERT INTO sample (id, value) VALUES (2, 'private')"))
        connection.execute(text("UPDATE sample SET value = 'updated' WHERE id = 2"))
        connection.execute(text("DELETE FROM sample WHERE id = 2"))
        observer.stop()
        connection.execute(text("SELECT value FROM sample WHERE id = 1"))

    assert observer.snapshot() == {
        "statement_count": 4,
        "write_statement_count": 3,
        "write_row_count": 3,
        "write_row_count_complete": True,
    }
    assert "private" not in repr(observer.snapshot())
    engine.dispose()


def test_observer_marks_failed_write_rowcount_incomplete(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'failed-observation.db'}")
    observer = BenchmarkSqlObservation(engine)

    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sample (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO sample (id) VALUES (1)"))

    with engine.connect() as connection:
        observer.start()
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO sample (id) VALUES (1)"))
        observer.stop()

    assert observer.snapshot() == {
        "statement_count": 1,
        "write_statement_count": 1,
        "write_row_count": 0,
        "write_row_count_complete": False,
    }
    engine.dispose()


def test_observer_accepts_successful_write_without_driver_rowcount(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'unknown-rowcount.db'}")
    observer = BenchmarkSqlObservation(engine)

    class Cursor:
        rowcount = -1

    observer.start()
    observer._after_cursor_execute(
        object(),
        Cursor(),
        "INSERT INTO sample VALUES (1)",
        (),
        SimpleNamespace(isinsert=True),
        False,
    )
    observer.stop()

    assert observer.snapshot() == {
        "statement_count": 0,
        "write_statement_count": 0,
        "write_row_count": 0,
        "write_row_count_complete": True,
    }
    engine.dispose()


def test_observer_recovers_insertmanyvalues_rowcount_without_driver_rowcount(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'insertmany-observation.db'}")
    observer = BenchmarkSqlObservation(engine)

    class Cursor:
        rowcount = -1

    observer.start()
    observer._after_cursor_execute(
        object(),
        Cursor(),
        "INSERT INTO sample VALUES (?, ?)",
        (),
        SimpleNamespace(isinsert=True, compiled_parameters=[{}, {}, {}]),
        False,
    )
    observer.stop()

    assert observer.snapshot() == {
        "statement_count": 0,
        "write_statement_count": 0,
        "write_row_count": 3,
        "write_row_count_complete": True,
    }
    engine.dispose()


def test_manual_copy_observer_uses_owner_connection_info(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'manual-observation.db'}")
    observer = BenchmarkSqlObservation(engine)

    with engine.connect() as connection:
        observer.start()
        attach_publication_manual_sql_observer(connection, observer)
        assert record_publication_sql(
            connection,
            duration_ms=1.0,
            is_manual_write=True,
            write_row_count=7,
        ) is False
        assert record_publication_sql(
            connection,
            duration_ms=1.0,
            is_manual_write=True,
            write_row_count=None,
        ) is False
        assert clear_publication_manual_sql_observer(connection) is observer
        observer.stop()

    assert observer.snapshot() == {
        "statement_count": 0,
        "write_statement_count": 2,
        "write_row_count": 7,
        "write_row_count_complete": False,
    }
    engine.dispose()
