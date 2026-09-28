"""PostgreSQL transaction contract for candidate chunk persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session

from shared.models.database.document import DocumentChunk
from shared.services.retrieval.publication_chunk_copy import insert_chunks_with_copy
from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)

_CREATE_CHUNK_TABLE = """
CREATE TEMP TABLE document_chunks (
    id varchar(36) PRIMARY KEY,
    chunk_id varchar(64) NOT NULL,
    user_id text NOT NULL,
    namespace varchar(255) NOT NULL,
    document_id varchar(36) NOT NULL,
    job_result_id varchar(36) NOT NULL,
    section_id varchar(36),
    chunk_type varchar(64) NOT NULL,
    content text,
    content_lexical_text text,
    path_lexical_text text,
    content_search_text text,
    path_search_text text,
    term_search_text text,
    source_chunk_path text,
    file_path text,
    chunk_metadata json,
    sort_order integer NOT NULL,
    created_at timestamp NOT NULL
) ON COMMIT PRESERVE ROWS
"""


def _build_chunks() -> list[DocumentChunk]:
    return [
        DocumentChunk(
            id="dchk_contract_1",
            chunk_id="chunk_contract_1",
            user_id="user_contract",
            namespace="default",
            document_id="doc_contract",
            job_result_id="job_contract",
            section_id=None,
            chunk_type="text",
            content="",
            content_lexical_text="",
            path_lexical_text=None,
            content_search_text="quoted, \"value\"\n汉字",
            path_search_text=None,
            term_search_text=r"\N",
            source_chunk_path=None,
            file_path=None,
            chunk_metadata={"quote": '"', "none": None, "unicode": "汉字"},
            sort_order=0,
        ),
        DocumentChunk(
            id="dchk_contract_2",
            chunk_id="chunk_contract_2",
            user_id="user_contract",
            namespace="default",
            document_id="doc_contract",
            job_result_id="job_contract",
            section_id="sec_contract",
            chunk_type="image",
            content=None,
            content_lexical_text=None,
            path_lexical_text="path",
            content_search_text=None,
            path_search_text="path",
            term_search_text="term",
            source_chunk_path="source",
            file_path="images/a.png",
            chunk_metadata=None,
            sort_order=1,
        ),
    ]


@pytest.fixture
def contract_database_url(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> Iterator[str]:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    yield get_contract_database_url()


def _read_rows(connection) -> list[tuple[object, ...]]:
    return list(
        connection.execute(
            text(
                "SELECT id, content, content_search_text, path_lexical_text, "
                "term_search_text, chunk_metadata FROM document_chunks ORDER BY id"
            )
        ).all()
    )


def test_candidate_chunk_copy_preserves_null_empty_json_and_rollback(
    contract_database_url: str,
) -> None:
    url = make_url(contract_database_url).set(drivername="postgresql+psycopg2")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text(_CREATE_CHUNK_TABLE))
            connection.commit()
            with Session(bind=connection) as session:
                insert_chunks_with_copy(session, _build_chunks())
                rows = _read_rows(connection)
                assert rows[0][1:5] == (
                    "",
                    'quoted, "value"\n汉字',
                    None,
                    r"\N",
                )
                assert rows[0][5] == {"quote": '"', "none": None, "unicode": "汉字"}
                assert rows[1][1] is None
                session.rollback()
            assert connection.scalar(text("SELECT count(*) FROM document_chunks")) == 0
    finally:
        engine.dispose()


async def test_candidate_chunk_copy_supports_asyncpg_owner_and_rollback(
    contract_database_url: str,
) -> None:
    url = make_url(contract_database_url).set(drivername="postgresql+asyncpg")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text(_CREATE_CHUNK_TABLE))
            await connection.commit()

            def copy_and_read(sync_connection) -> list[tuple[object, ...]]:
                with Session(bind=sync_connection) as session:
                    session.execute(text("SELECT 1"))
                    insert_chunks_with_copy(session, _build_chunks())
                    rows = _read_rows(sync_connection)
                    session.rollback()
                    return rows

            rows = await connection.run_sync(copy_and_read)
            assert rows[0][1] == ""
            assert rows[0][2] == 'quoted, "value"\n汉字'
            assert rows[0][3] is None
            assert rows[1][1] is None
            assert await connection.scalar(
                text("SELECT count(*) FROM document_chunks")
            ) == 0
    finally:
        await engine.dispose()
