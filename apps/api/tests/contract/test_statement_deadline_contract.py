"""Owned PostgreSQL checks for API and worker database deadlines."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from importlib import import_module
import time
from typing import Any

import gevent
import psycopg2
import pytest
from loguru import logger
from psycopg2 import errors
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError

from shared.core.statement_deadline import StatementDeadline
from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    clear_application_modules,
    configure_contract_environment,
    get_contract_database_url,
)

Connect = Callable[[], Any]


@pytest.mark.asyncio
async def testApiTransactionTimeouts(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    url: URL = make_url(get_contract_database_url()).set(database="postgres")
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    clear_application_modules()
    database = import_module("shared.core.database")
    try:
        async with database.engine.connect() as connection:
            async with connection.begin():
                await connection.exec_driver_sql("SET statement_timeout = 0")
                await connection.exec_driver_sql("SET idle_in_transaction_session_timeout = 0")

            async with connection.begin():
                await connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                assert await connection.scalar(text("SHOW transaction_isolation")) == "repeatable read"
                assert await connection.scalar(text("SHOW transaction_read_only")) == "on"
                assert await connection.scalar(text("SHOW statement_timeout")) == "30s"
                assert await connection.scalar(text("SHOW idle_in_transaction_session_timeout")) == "1min"

        async with database.get_db_context_with_timeouts(120, 6) as session:
            await session.execute(text("SET statement_timeout = 0"))
            await session.commit()
            assert await session.scalar(text("SHOW statement_timeout")) == "120ms"
            with pytest.raises(DBAPIError) as error:
                await session.execute(text("SELECT pg_sleep(2)"))
            assert getattr(error.value.orig, "sqlstate", None) == "57014"
            await session.rollback()
            assert await session.scalar(text("SHOW statement_timeout")) == "120ms"
            assert await session.scalar(text("SELECT 1")) == 1
    finally:
        await database.dispose_all_engines()


@pytest.fixture
def greenDatabase(postgresql_proc: PostgreSQLProcess) -> Iterator[tuple[Connect, str, list[str]]]:
    url: str = (f"postgresql+psycopg2://{postgresql_proc.user}:{postgresql_proc.password or ''}"
        f"@{postgresql_proc.host}:{postgresql_proc.port}/postgres")

    def connect() -> Any:
        return psycopg2.connect(host=postgresql_proc.host, port=postgresql_proc.port,
            user=postgresql_proc.user, password=postgresql_proc.password, dbname="postgres")

    messages: list[str] = []
    sinkId: int = logger.add(lambda message: messages.append(str(message.record["message"])), level="WARNING")
    try:
        yield connect, url, messages
    finally:
        StatementDeadline.uninstall()
        logger.remove(sinkId)


def test_statement_past_its_deadline_is_cancelled_and_connection_is_reusable(
    greenDatabase: tuple[Connect, str, list[str]],
) -> None:
    connect, _, messages = greenDatabase
    StatementDeadline.install(warningSeconds=0.2, timeoutSeconds=0.5)
    connection = connect()
    started: float = time.monotonic()
    with pytest.raises(errors.QueryCanceled):
        connection.cursor().execute("select pg_sleep(5)")
    assert time.monotonic() - started < 2
    connection.rollback()
    cursor = connection.cursor()
    cursor.execute("select 1")
    assert cursor.fetchone() == (1,)
    connection.close()
    assert [message.split(":")[0] for message in messages] == [
        "Worker database statement is slow", "Worker database statement cancelled at its deadline"]


def test_warning_only_mode_never_cancels(greenDatabase: tuple[Connect, str, list[str]]) -> None:
    connect, _, messages = greenDatabase
    StatementDeadline.install(warningSeconds=0.2, timeoutSeconds=0)
    connection = connect()
    cursor = connection.cursor()
    cursor.execute("select 7 from pg_sleep(0.8)")
    assert cursor.fetchone() == (7,)
    connection.close()
    assert len(messages) == 1 and "is slow" in messages[0]
    assert "timeout_s=off" in messages[0]


def test_fast_statements_and_connection_setup_are_never_bounded(
    greenDatabase: tuple[Connect, str, list[str]],
) -> None:
    connect, _, messages = greenDatabase
    # Thresholds far below a connection handshake: setup must still complete.
    StatementDeadline.install(warningSeconds=0.0001, timeoutSeconds=0.0002)
    connection = connect()
    StatementDeadline.install(warningSeconds=5, timeoutSeconds=10)
    cursor = connection.cursor()
    for _ in range(20):
        cursor.execute("select 1")
    connection.commit()
    connection.close()
    assert messages == []


def test_slow_warning_names_the_sqlalchemy_statement(greenDatabase: tuple[Connect, str, list[str]]) -> None:
    _, url, messages = greenDatabase
    StatementDeadline.install(warningSeconds=0.1, timeoutSeconds=0)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text("select pg_sleep(0.4) as slow_marker"))
    finally:
        engine.dispose()
    assert len(messages) == 1 and "slow_marker" in messages[0]


def test_cancelled_greenlet_does_not_stall_another(greenDatabase: tuple[Connect, str, list[str]]) -> None:
    connect, _, _ = greenDatabase
    StatementDeadline.install(warningSeconds=0.2, timeoutSeconds=0.5)
    outcomes: dict[str, str] = {}

    def run(label: str, sql: str) -> None:
        connection = connect()
        try:
            connection.cursor().execute(sql)
            outcomes[label] = "finished"
        except errors.QueryCanceled:
            outcomes[label] = "cancelled"
        finally:
            connection.close()

    started: float = time.monotonic()
    gevent.joinall([gevent.spawn(run, "slow", "select pg_sleep(5)"),
        gevent.spawn(run, "fast", "select pg_sleep(0.1)")])
    assert outcomes == {"slow": "cancelled", "fast": "finished"}
    assert time.monotonic() - started < 2


def test_uninstall_restores_blocking_mode_for_later_code(greenDatabase: tuple[Connect, str, list[str]]) -> None:
    from psycopg2.extensions import get_wait_callback

    connect, _, messages = greenDatabase
    StatementDeadline.install(warningSeconds=0.1, timeoutSeconds=0.2)
    StatementDeadline.uninstall()
    assert get_wait_callback() is None
    connection = connect()
    cursor = connection.cursor()
    cursor.execute("select 7 from pg_sleep(0.4)")
    assert cursor.fetchone() == (7,)
    connection.close()
    assert messages == []
