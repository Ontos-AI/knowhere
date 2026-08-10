"""Contract tests for the BM25 channel Postgres FTS prefilter.

These run against a real Postgres so the prefilter is validated against the
same generated tsvector columns and GIN indexes production uses. A pure-Python
fake would not catch a mismatch between the query configuration and the one
the columns were generated with.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from shared.services.retrieval.search.channels import content_channel, path_channel
from shared.testing.contract_runtime import PostgreSQLProcess
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import text

_SCHEMA = """
CREATE TABLE documents (
    document_id TEXT PRIMARY KEY,
    user_id TEXT,
    namespace TEXT,
    status TEXT,
    current_job_result_id INTEGER,
    source_file_name TEXT
);
CREATE TABLE job_results (id INTEGER PRIMARY KEY, job_id TEXT);
CREATE TABLE document_sections (section_id TEXT PRIMARY KEY, section_path TEXT);
CREATE TABLE document_chunks (
    id SERIAL PRIMARY KEY,
    chunk_id TEXT,
    document_id TEXT,
    section_id TEXT,
    chunk_type TEXT,
    content TEXT,
    source_chunk_path TEXT,
    file_path TEXT,
    chunk_metadata JSONB,
    job_result_id INTEGER,
    sort_order INTEGER,
    content_search_text TEXT,
    content_search_tsv TSVECTOR GENERATED ALWAYS AS
        (to_tsvector('simple', COALESCE(content_search_text, ''))) STORED,
    path_search_text TEXT,
    path_search_tsv TSVECTOR GENERATED ALWAYS AS
        (to_tsvector('simple', COALESCE(path_search_text, ''))) STORED,
    term_search_text TEXT
);
CREATE INDEX idx_chunk_content_search_tsv ON document_chunks USING GIN (content_search_tsv);
CREATE INDEX idx_chunk_path_search_tsv ON document_chunks USING GIN (path_search_tsv);
"""

_NOISE_ROWS = 300


@pytest_asyncio.fixture
async def seeded_session(
    postgresql_proc: PostgreSQLProcess,
) -> AsyncGenerator[AsyncSession, None]:
    dsn = (
        f"postgresql+asyncpg://{postgresql_proc.user}@"
        f"{postgresql_proc.host}:{postgresql_proc.port}/postgres"
    )
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS bm25_fts CASCADE"))
        await conn.execute(text("CREATE SCHEMA bm25_fts"))
        await conn.execute(text("SET search_path TO bm25_fts"))
        for statement in filter(None, (s.strip() for s in _SCHEMA.split(";"))):
            await conn.execute(text(statement))
        await conn.execute(text("INSERT INTO job_results VALUES (1, 'job1')"))
        await conn.execute(
            text(
                "INSERT INTO documents VALUES "
                "('d1', 'u1', 'ns1', 'active', 1, 'sample.pdf')"
            )
        )
        await conn.execute(
            text("INSERT INTO document_sections VALUES ('s1', '/root')")
        )
        await conn.execute(
            text(
                "INSERT INTO document_chunks "
                "(chunk_id, document_id, section_id, chunk_type, content, "
                " job_result_id, sort_order, content_search_text, path_search_text) "
                "VALUES "
                "('hit-en', 'd1', 's1', 'text', 'body', 1, 1, "
                " 'alpha beta gamma', 'invoices alpha'), "
                "('hit-cjk', 'd1', 's1', 'text', 'body', 1, 2, "
                " '合同 条款 甲方', '合同 目录')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO document_chunks "
                "(chunk_id, document_id, section_id, chunk_type, content, "
                " job_result_id, sort_order, content_search_text, path_search_text) "
                "SELECT 'noise-' || i, 'd1', 's1', 'text', 'body', 1, i + 10, "
                "       'filler unrelated wording ' || i, 'misc path ' || i "
                "FROM generate_series(1, :noise) AS i"
            ),
            {"noise": _NOISE_ROWS},
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        await session.execute(text("SET search_path TO bm25_fts"))
        yield session
    await engine.dispose()


async def _content_hits(session: AsyncSession, query: str) -> list[str]:
    rows = await content_channel(
        session,
        user_id="u1",
        namespace="ns1",
        query=query,
        top_k=50,
        exclude_document_ids=[],
        exclude_sections=[],
    )
    return [str(row["chunk_id"]) for row in rows]


@pytest.mark.asyncio
async def test_content_channel_returns_only_query_matching_chunks(
    seeded_session: AsyncSession,
) -> None:
    # The corpus holds hundreds of unrelated chunks. Before the prefilter every
    # one of them was loaded into Python for BM25 scoring.
    assert await _content_hits(seeded_session, "alpha") == ["hit-en"]


@pytest.mark.asyncio
async def test_content_channel_matches_cjk_tokens(
    seeded_session: AsyncSession,
) -> None:
    assert await _content_hits(seeded_session, "合同") == ["hit-cjk"]


@pytest.mark.asyncio
async def test_content_channel_uses_or_semantics_across_tokens(
    seeded_session: AsyncSession,
) -> None:
    # A row matching any single query token must survive, matching how the
    # Python BM25 ranker admits rows.
    hits = await _content_hits(seeded_session, "alpha 合同")
    assert sorted(hits) == ["hit-cjk", "hit-en"]


@pytest.mark.asyncio
async def test_tsquery_operators_in_query_do_not_change_filter_shape(
    seeded_session: AsyncSession,
) -> None:
    # Tokens are lexed by Postgres as data. If operators leaked into tsquery
    # syntax, "alpha & zzzz" would AND and drop the row.
    assert await _content_hits(seeded_session, "alpha & zzzz") == ["hit-en"]
    assert await _content_hits(seeded_session, "!alpha") == ["hit-en"]


@pytest.mark.asyncio
async def test_query_matching_nothing_returns_no_rows(
    seeded_session: AsyncSession,
) -> None:
    # The fallback re-runs the unfiltered scan, and BM25 then scores no row
    # above zero, so the channel still yields nothing.
    assert await _content_hits(seeded_session, "zzzznomatch") == []


@pytest.mark.asyncio
async def test_path_channel_prefilters_on_path_search_tsv(
    seeded_session: AsyncSession,
) -> None:
    rows = await path_channel(
        seeded_session,
        user_id="u1",
        namespace="ns1",
        query="invoices",
        top_k=50,
        exclude_document_ids=[],
        exclude_sections=[],
    )
    assert [str(row["chunk_id"]) for row in rows] == ["hit-en"]


@pytest.mark.asyncio
async def test_exclusions_still_apply_under_the_prefilter(
    seeded_session: AsyncSession,
) -> None:
    rows = await content_channel(
        seeded_session,
        user_id="u1",
        namespace="ns1",
        query="alpha",
        top_k=50,
        exclude_document_ids=["d1"],
        exclude_sections=[],
    )
    assert rows == []
