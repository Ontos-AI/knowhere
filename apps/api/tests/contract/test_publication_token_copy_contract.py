"""PostgreSQL transaction contract for candidate token persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from psycogreen.gevent import patch_psycopg
from psycopg2.extensions import get_wait_callback, set_wait_callback
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session

from shared.services.retrieval.publication_token_copy import insert_token_rows_with_copy
from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)

_CREATE_TOKEN_TABLE = """
CREATE TEMP TABLE document_map_unit_tokens (
    id varchar(36) PRIMARY KEY,
    map_unit_id varchar(160) NOT NULL,
    channel varchar(16) NOT NULL,
    token text NOT NULL,
    token_hash varchar(64) NOT NULL,
    frequency integer NOT NULL
) ON COMMIT PRESERVE ROWS
"""
_TOKEN_ROWS: list[dict[str, object]] = [
    {
        "id": "dmut_contract_1",
        "map_unit_id": "dmu_contract_1",
        "channel": "content",
        "token": 'quoted, "token"\n汉字',
        "token_hash": "a" * 64,
        "frequency": 3,
    },
    {
        "id": "dmut_contract_2",
        "map_unit_id": "dmu_contract_1",
        "channel": "path",
        "token": "ordinary",
        "token_hash": "b" * 64,
        "frequency": 1,
    },
]


@pytest.fixture
def contract_database_url(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> Iterator[str]:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    yield get_contract_database_url()


@pytest.mark.parametrize("has_cooperative_callback", [False, True])
def test_candidate_copy_preserves_rows_and_rollback_with_psycopg2(
    contract_database_url: str,
    has_cooperative_callback: bool,
) -> None:
    original_callback = get_wait_callback()
    if has_cooperative_callback:
        patch_psycopg()
    else:
        set_wait_callback(None)
    expected_callback = get_wait_callback()
    url = make_url(contract_database_url).set(drivername="postgresql+psycopg2")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text(_CREATE_TOKEN_TABLE))
            connection.commit()
            with Session(bind=connection) as session:
                insert_token_rows_with_copy(session, _TOKEN_ROWS)
                assert get_wait_callback() is expected_callback
                values = connection.execute(
                    text("SELECT token, frequency FROM document_map_unit_tokens ORDER BY id")
                ).all()
                assert values == [('quoted, "token"\n汉字', 3), ("ordinary", 1)]
            connection.rollback()
            assert connection.scalar(
                text("SELECT count(*) FROM document_map_unit_tokens")
            ) == 0
    finally:
        engine.dispose()
        set_wait_callback(original_callback)


async def test_candidate_copy_preserves_rows_and_rollback_with_asyncpg(
    contract_database_url: str,
) -> None:
    url = make_url(contract_database_url).set(drivername="postgresql+asyncpg")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text(_CREATE_TOKEN_TABLE))
            await connection.commit()

            def copy_and_read(sync_connection) -> list[tuple[str, int]]:
                with Session(bind=sync_connection) as session:
                    insert_token_rows_with_copy(session, _TOKEN_ROWS)
                    return [
                        (str(row.token), int(row.frequency))
                        for row in sync_connection.execute(
                            text(
                                "SELECT token, frequency FROM document_map_unit_tokens ORDER BY id"
                            )
                        )
                    ]

            transaction = await connection.begin()
            await connection.execute(text("SELECT 1"))
            values = await connection.run_sync(copy_and_read)
            assert values == [('quoted, "token"\n汉字', 3), ("ordinary", 1)]
            await transaction.rollback()
            assert await connection.scalar(
                text("SELECT count(*) FROM document_map_unit_tokens")
            ) == 0
    finally:
        await engine.dispose()


async def test_candidate_token_writes_complete_under_per_command_budget(
    contract_database_url: str,
) -> None:
    """Slow token writes must stay atomic without requiring one long command."""
    url = make_url(contract_database_url).set(drivername="postgresql+asyncpg")
    engine = create_async_engine(
        url,
        connect_args={
            "command_timeout": 3,
            "server_settings": {"statement_timeout": "3000"},
        },
    )
    rows: list[dict[str, object]] = [
        {**_TOKEN_ROWS[0], "id": f"dmut_slow_{index}"}
        for index in range(2_501)
    ]
    try:
        async with engine.connect() as connection:
            await connection.execute(text(_CREATE_TOKEN_TABLE))
            await connection.execute(text("""
                CREATE FUNCTION pg_temp.delay_token_write() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    PERFORM pg_sleep((SELECT count(*) FROM inserted_tokens) * 0.002);
                    RETURN NULL;
                END $$
            """))
            await connection.execute(text("""
                CREATE TRIGGER delay_token_write AFTER INSERT
                ON document_map_unit_tokens
                REFERENCING NEW TABLE AS inserted_tokens
                FOR EACH STATEMENT EXECUTE FUNCTION pg_temp.delay_token_write()
            """))
            await connection.commit()
            transaction = await connection.begin()
            # Publication has already written its document before token persistence.
            await connection.execute(text("SELECT 1"))

            def write_tokens(sync_connection: Connection) -> None:
                with Session(bind=sync_connection) as session:
                    insert_token_rows_with_copy(session, rows)

            await connection.run_sync(write_tokens)
            assert await connection.scalar(
                text("SELECT count(*) FROM document_map_unit_tokens")
            ) == len(rows)
            await transaction.rollback()
            assert await connection.scalar(
                text("SELECT count(*) FROM document_map_unit_tokens")
            ) == 0
    finally:
        await engine.dispose()
