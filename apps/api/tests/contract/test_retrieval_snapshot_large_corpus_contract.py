from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast
from uuid import uuid4

import pytest
from httpx import AsyncClient

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult
from shared.services.retrieval.nav_snapshot import load_nav_snapshot
from sqlalchemy import Executable, Result, select, text
from sqlalchemy.engine import Row
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.sql.selectable import Select
from tests.support.contract_database import ContractDatabase
from shared.testing.contract_runtime import get_contract_database_url


_USER_ID = "local-dev-user"
_DOCUMENT_COUNT = 100
_CHUNKS_PER_DOCUMENT = 600
_SECTIONS_PER_DOCUMENT = 8
_CONTENT_BYTES = 2048
_TOTAL_CHUNKS = _DOCUMENT_COUNT * _CHUNKS_PER_DOCUMENT
LegacySnapshotRow = Row[
    tuple[
        str,
        str,
        str | None,
        str,
        str,
        int,
        str,
        str | None,
        dict[str, object],
        str | None,
        str | None,
    ]
]


class _PublishingSession:
    def __init__(
        self,
        session: AsyncSession,
        publish_revision: Callable[[], Awaitable[None]],
    ) -> None:
        self._session = session
        self._publish_revision = publish_revision
        self._has_published = False

    async def execute(self, statement: Executable) -> Result[tuple[object, ...]]:
        result = await self._session.execute(statement)
        if not self._has_published:
            self._has_published = True
            await self._publish_revision()
        return cast(Result[tuple[object, ...]], result)


class _CountingSession:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.chunk_query_count = 0

    async def execute(self, statement: Executable) -> Result[tuple[object, ...]]:
        if isinstance(statement, Select):
            selected_tables = {
                getattr(table, "name", "")
                for table in statement.get_final_froms()
            }
        else:
            selected_tables = set()
        if "document_chunks" in selected_tables:
            self.chunk_query_count += 1
        result = await self._session.execute(statement)
        return cast(Result[tuple[object, ...]], result)


@asynccontextmanager
async def _contract_db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(get_contract_database_url(), future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


async def _seed_large_retrieval_corpus(namespace: str) -> None:
    await ContractDatabase.execute(
        """
        INSERT INTO jobs (
            job_id, user_id, job_type, status, source_type, version,
            webhook_enabled, created_at, updated_at, credits_charged, billing_status
        )
        SELECT
            'job_lg_' || i,
            :user_id,
            'document_ingestion',
            'done',
            'file',
            0,
            false,
            NOW(),
            NOW(),
            0,
            'skipped'
        FROM generate_series(1, :document_count) AS values(i)
        """,
        {"user_id": _USER_ID, "document_count": _DOCUMENT_COUNT},
    )
    await ContractDatabase.execute(
        """
        INSERT INTO documents (
            document_id, user_id, namespace, status, current_job_result_id,
            source_file_name, parse_track, created_at, updated_at
        )
        SELECT
            'doc_lg_' || i,
            :user_id,
            :namespace,
            'active',
            NULL,
            'large-corpus-' || i || '.pdf',
            'chunk',
            NOW(),
            NOW()
        FROM generate_series(1, :document_count) AS values(i)
        """,
        {
            "user_id": _USER_ID,
            "namespace": namespace,
            "document_count": _DOCUMENT_COUNT,
        },
    )
    await ContractDatabase.execute(
        """
        INSERT INTO job_results (
            id, job_id, document_id, delivery_mode, created_at, updated_at
        )
        SELECT
            'result_lg_' || i,
            'job_lg_' || i,
            'doc_lg_' || i,
            'inline',
            NOW(),
            NOW()
        FROM generate_series(1, :document_count) AS values(i)
        """,
        {"document_count": _DOCUMENT_COUNT},
    )
    await ContractDatabase.execute(
        """
        UPDATE documents
        SET current_job_result_id = 'result_lg_' || i
        FROM generate_series(1, :document_count) AS values(i)
        WHERE documents.document_id = 'doc_lg_' || i
        """,
        {"document_count": _DOCUMENT_COUNT},
    )
    await ContractDatabase.execute(
        """
        INSERT INTO document_sections (
            section_id, user_id, namespace, document_id, job_result_id,
            section_path, section_title, section_level, sort_order, created_at
        )
        SELECT
            'section_lg_' || i || '_' || section_number,
            :user_id,
            :namespace,
            'doc_lg_' || i,
            'result_lg_' || i,
            'large-corpus-' || i || '/section/' || section_number,
            'section-' || section_number,
            1,
            section_number,
            NOW()
        FROM generate_series(1, :document_count) AS values(i)
        CROSS JOIN generate_series(1, :sections_per_document) AS sections(section_number)
        """,
        {
            "user_id": _USER_ID,
            "namespace": namespace,
            "document_count": _DOCUMENT_COUNT,
            "sections_per_document": _SECTIONS_PER_DOCUMENT,
        },
    )
    await ContractDatabase.execute(
        """
        INSERT INTO document_chunks (
            id, chunk_id, user_id, namespace, document_id, job_result_id,
            section_id, chunk_type, content, content_lexical_text,
            path_lexical_text, content_search_text, path_search_text,
            term_search_text, source_chunk_path, chunk_metadata, sort_order,
            created_at
        )
        SELECT
            'dchunk_lg_' || document_number || '_' || chunk_number,
            'chunk_lg_' || document_number || '_' || chunk_number,
            :user_id,
            :namespace,
            'doc_lg_' || document_number,
            'result_lg_' || document_number,
            'section_lg_' || document_number || '_' || section_number,
            'text',
            repeat(md5(document_number::text || ':' || chunk_number::text), 64),
            repeat(md5(document_number::text || ':' || chunk_number::text), 64),
            'large-corpus-' || document_number || '/section/' || section_number || '/' || chunk_number,
            repeat(md5(document_number::text || ':' || chunk_number::text), 64),
            'large-corpus-' || document_number || '/section/' || section_number || '/' || chunk_number,
            repeat(md5(document_number::text || ':' || chunk_number::text), 64),
            'large-corpus-' || document_number || '/section/' || section_number || '/' || chunk_number,
            json_build_object(
                'tokens', ARRAY['benchmark', 'retrieval', 'document', document_number::text],
                'keywords', ARRAY['benchmark', 'production-shaped'],
                'summary', repeat('payload ', 32)
            )::json,
            chunk_number,
            NOW()
        FROM generate_series(1, :document_count) AS documents(document_number)
        CROSS JOIN generate_series(1, :chunks_per_document) AS chunks(chunk_number)
        CROSS JOIN LATERAL (
            SELECT ((chunk_number - 1) % :sections_per_document) + 1 AS section_number
        ) AS section_values
        """,
        {
            "user_id": _USER_ID,
            "namespace": namespace,
            "document_count": _DOCUMENT_COUNT,
            "chunks_per_document": _CHUNKS_PER_DOCUMENT,
            "sections_per_document": _SECTIONS_PER_DOCUMENT,
        },
    )


async def _seed_republished_document(namespace: str) -> tuple[str, str]:
    identifier = uuid4().hex[:8]
    document_id = f"doc_race_{identifier}"
    old_result_id = f"result_race_old_{identifier}"
    new_result_id = f"result_race_new_{identifier}"
    await ContractDatabase.execute(
        """
        INSERT INTO jobs (
            job_id, user_id, job_type, status, source_type, version,
            webhook_enabled, created_at, updated_at, credits_charged, billing_status
        ) VALUES
            (:old_job_id, :user_id, 'document_ingestion', 'done', 'file', 0,
             false, NOW(), NOW(), 0, 'skipped'),
            (:new_job_id, :user_id, 'document_ingestion', 'done', 'file', 0,
             false, NOW(), NOW(), 0, 'skipped')
        """,
        {
            "old_job_id": f"job_race_old_{identifier}",
            "new_job_id": f"job_race_new_{identifier}",
            "user_id": _USER_ID,
        },
    )
    await ContractDatabase.execute(
        """
        INSERT INTO documents (
            document_id, user_id, namespace, status, current_job_result_id,
            source_file_name, parse_track, created_at, updated_at
        ) VALUES (
            :document_id, :user_id, :namespace, 'active', NULL,
            'republished.pdf', 'chunk', NOW(), NOW()
        )
        """,
        {
            "document_id": document_id,
            "user_id": _USER_ID,
            "namespace": namespace,
        },
    )
    await ContractDatabase.execute(
        """
        INSERT INTO job_results (
            id, job_id, document_id, delivery_mode, created_at, updated_at
        ) VALUES
            (:old_result_id, :old_job_id, :document_id, 'inline', NOW(), NOW()),
            (:new_result_id, :new_job_id, :document_id, 'inline', NOW(), NOW())
        """,
        {
            "old_result_id": old_result_id,
            "new_result_id": new_result_id,
            "old_job_id": f"job_race_old_{identifier}",
            "new_job_id": f"job_race_new_{identifier}",
            "document_id": document_id,
        },
    )
    await ContractDatabase.execute(
        """
        INSERT INTO document_sections (
            section_id, user_id, namespace, document_id, job_result_id,
            section_path, section_title, section_level, sort_order, created_at
        ) VALUES
            (:old_section_id, :user_id, :namespace, :document_id, :old_result_id,
             'republished.pdf/old', 'old', 1, 1, NOW()),
            (:new_section_id, :user_id, :namespace, :document_id, :new_result_id,
             'republished.pdf/new', 'new', 1, 1, NOW())
        """,
        {
            "old_section_id": f"section_race_old_{identifier}",
            "new_section_id": f"section_race_new_{identifier}",
            "user_id": _USER_ID,
            "namespace": namespace,
            "document_id": document_id,
            "old_result_id": old_result_id,
            "new_result_id": new_result_id,
        },
    )
    await ContractDatabase.execute(
        """
        INSERT INTO document_chunks (
            id, chunk_id, user_id, namespace, document_id, job_result_id,
            section_id, chunk_type, content, source_chunk_path,
            chunk_metadata, sort_order, created_at
        ) VALUES
            (:old_id, :old_chunk_id, :user_id, :namespace, :document_id,
             :old_result_id, :old_section_id, 'text', 'old content',
             'republished.pdf/old/chunk', '{}'::json, 1, NOW()),
            (:new_id, :new_chunk_id, :user_id, :namespace, :document_id,
             :new_result_id, :new_section_id, 'text', 'new content',
             'republished.pdf/new/chunk', '{}'::json, 1, NOW())
        """,
        {
            "old_id": f"dchunk_race_old_{identifier}",
            "new_id": f"dchunk_race_new_{identifier}",
            "old_chunk_id": f"chunk_race_old_{identifier}",
            "new_chunk_id": f"chunk_race_new_{identifier}",
            "user_id": _USER_ID,
            "namespace": namespace,
            "document_id": document_id,
            "old_result_id": old_result_id,
            "new_result_id": new_result_id,
            "old_section_id": f"section_race_old_{identifier}",
            "new_section_id": f"section_race_new_{identifier}",
        },
    )
    await ContractDatabase.execute(
        """
        UPDATE documents
        SET current_job_result_id = :old_result_id
        WHERE document_id = :document_id
        """,
        {"old_result_id": old_result_id, "document_id": document_id},
    )
    return document_id, new_result_id


async def _seed_many_small_documents(namespace: str, *, document_count: int) -> None:
    identifier = uuid4().hex[:8]
    await ContractDatabase.execute(
        """
        INSERT INTO jobs (
            job_id, user_id, job_type, status, source_type, version,
            webhook_enabled, created_at, updated_at, credits_charged, billing_status
        )
        SELECT
            'job_batch_' || :identifier || '_' || i,
            :user_id,
            'document_ingestion',
            'done',
            'file',
            0,
            false,
            NOW(),
            NOW(),
            0,
            'skipped'
        FROM generate_series(1, :document_count) AS values(i)
        """,
        {
            "identifier": identifier,
            "user_id": _USER_ID,
            "document_count": document_count,
        },
    )
    await ContractDatabase.execute(
        """
        INSERT INTO documents (
            document_id, user_id, namespace, status, current_job_result_id,
            source_file_name, parse_track, created_at, updated_at
        )
        SELECT
            'doc_batch_' || :identifier || '_' || i,
            :user_id,
            :namespace,
            'active',
            NULL,
            'batch-' || i || '.pdf',
            'chunk',
            NOW(),
            NOW()
        FROM generate_series(1, :document_count) AS values(i)
        """,
        {
            "identifier": identifier,
            "user_id": _USER_ID,
            "namespace": namespace,
            "document_count": document_count,
        },
    )
    await ContractDatabase.execute(
        """
        INSERT INTO job_results (
            id, job_id, document_id, delivery_mode, created_at, updated_at
        )
        SELECT
            'result_batch_' || :identifier || '_' || i,
            'job_batch_' || :identifier || '_' || i,
            'doc_batch_' || :identifier || '_' || i,
            'inline',
            NOW(),
            NOW()
        FROM generate_series(1, :document_count) AS values(i)
        """,
        {"identifier": identifier, "document_count": document_count},
    )
    await ContractDatabase.execute(
        """
        UPDATE documents
        SET current_job_result_id = 'result_batch_' || :identifier || '_' || i
        FROM generate_series(1, :document_count) AS values(i)
        WHERE document_id = 'doc_batch_' || :identifier || '_' || i
        """,
        {"identifier": identifier, "document_count": document_count},
    )
    await ContractDatabase.execute(
        """
        INSERT INTO document_sections (
            section_id, user_id, namespace, document_id, job_result_id,
            section_path, section_title, section_level, sort_order, created_at
        )
        SELECT
            'section_batch_' || :identifier || '_' || i,
            :user_id,
            :namespace,
            'doc_batch_' || :identifier || '_' || i,
            'result_batch_' || :identifier || '_' || i,
            'batch-' || i || '.pdf/section',
            'section',
            1,
            1,
            NOW()
        FROM generate_series(1, :document_count) AS values(i)
        """,
        {
            "identifier": identifier,
            "user_id": _USER_ID,
            "namespace": namespace,
            "document_count": document_count,
        },
    )
    await ContractDatabase.execute(
        """
        INSERT INTO document_chunks (
            id, chunk_id, user_id, namespace, document_id, job_result_id,
            section_id, chunk_type, content, source_chunk_path,
            chunk_metadata, sort_order, created_at
        )
        SELECT
            'dchunk_batch_' || :identifier || '_' || i,
            'chunk_batch_' || :identifier || '_' || i,
            :user_id,
            :namespace,
            'doc_batch_' || :identifier || '_' || i,
            'result_batch_' || :identifier || '_' || i,
            'section_batch_' || :identifier || '_' || i,
            'text',
            'content-' || i,
            'batch-' || i || '.pdf/section/chunk',
            '{}'::json,
            1,
            NOW()
        FROM generate_series(1, :document_count) AS values(i)
        """,
        {
            "identifier": identifier,
            "user_id": _USER_ID,
            "namespace": namespace,
            "document_count": document_count,
        },
    )


async def test_snapshot_keeps_sections_and_chunks_on_the_captured_revision(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    namespace = f"revision-race-{uuid4().hex[:8]}"
    async with developer_api_client_factory():
        document_id, new_result_id = await _seed_republished_document(namespace)

        async def publish_new_revision() -> None:
            await ContractDatabase.execute(
                """
                UPDATE documents
                SET current_job_result_id = :new_result_id
                WHERE document_id = :document_id
                """,
                {"new_result_id": new_result_id, "document_id": document_id},
            )

        async with _contract_db_session() as db:
            publishing_db = cast(
                AsyncSession,
                _PublishingSession(db, publish_new_revision),
            )
            snapshot = await load_nav_snapshot(
                publishing_db,
                user_id=_USER_ID,
                namespace=namespace,
            )

    section_ids = list(snapshot.provider.children(document_id))
    chunks = [
        chunk
        for section_id in section_ids
        for chunk in snapshot.provider.self_units(section_id)
    ]
    assert [chunk.content for chunk in chunks] == ["old content"]
    assert snapshot.chunk_ref_index[chunks[0].chunk_id]["section_path"] == (
        "republished.pdf/old"
    )


async def test_snapshot_batches_chunks_across_many_small_documents(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    document_count = 25
    namespace = f"batch-documents-{uuid4().hex[:8]}"
    async with developer_api_client_factory():
        await _seed_many_small_documents(namespace, document_count=document_count)
        async with _contract_db_session() as db:
            counting_db = _CountingSession(db)
            snapshot = await load_nav_snapshot(
                cast(AsyncSession, counting_db),
                user_id=_USER_ID,
                namespace=namespace,
            )

    assert len(snapshot.document_ids) == document_count
    assert counting_db.chunk_query_count == 1


async def _load_legacy_rows(
    namespace: str,
    *,
    statement_timeout_ms: int | None = None,
) -> list[LegacySnapshotRow]:
    stmt = (
        select(
            Document.document_id,
            DocumentChunk.chunk_id,
            DocumentChunk.section_id,
            DocumentChunk.chunk_type,
            DocumentChunk.content,
            DocumentChunk.sort_order,
            DocumentChunk.source_chunk_path,
            DocumentChunk.file_path,
            DocumentChunk.chunk_metadata,
            DocumentSection.section_path,
            JobResult.job_id,
        )
        .join(
            DocumentChunk,
            (DocumentChunk.document_id == Document.document_id)
            & (DocumentChunk.job_result_id == Document.current_job_result_id),
        )
        .outerjoin(
            DocumentSection, DocumentSection.section_id == DocumentChunk.section_id
        )
        .outerjoin(JobResult, JobResult.id == DocumentChunk.job_result_id)
        .where(Document.user_id == _USER_ID)
        .where(Document.namespace == namespace)
        .where(Document.status == "active")
        .order_by(
            Document.document_id, DocumentChunk.sort_order, DocumentChunk.chunk_id
        )
    )
    async with _contract_db_session() as db:
        if statement_timeout_ms is not None:
            await db.execute(
                text("SELECT set_config('statement_timeout', :timeout_value, true)"),
                {"timeout_value": f"{statement_timeout_ms}ms"},
            )
        rows: list[LegacySnapshotRow] = list((await db.execute(stmt)).all())
        return rows


async def _explain_snapshot_query(namespace: str) -> dict[str, Any]:
    row = await ContractDatabase.fetch_one(
        """
        EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
        SELECT
            d.document_id,
            c.chunk_id,
            c.section_id,
            c.chunk_type,
            c.content,
            c.sort_order,
            c.source_chunk_path,
            c.file_path,
            c.chunk_metadata,
            s.section_path,
            r.job_id
        FROM documents AS d
        JOIN document_chunks AS c
          ON c.document_id = d.document_id
         AND c.job_result_id = d.current_job_result_id
        LEFT JOIN document_sections AS s ON s.section_id = c.section_id
        LEFT JOIN job_results AS r ON r.id = c.job_result_id
        WHERE d.user_id = :user_id
          AND d.namespace = :namespace
          AND d.status = 'active'
        ORDER BY d.document_id, c.sort_order, c.chunk_id
        """,
        {"user_id": _USER_ID, "namespace": namespace},
    )
    if row is None:
        raise AssertionError("EXPLAIN returned no plan")
    plan_value = next(iter(row.values()))
    if not isinstance(plan_value, list) or not plan_value:
        raise AssertionError("EXPLAIN returned an unexpected plan shape")
    plan = plan_value[0]
    if not isinstance(plan, dict):
        raise AssertionError("EXPLAIN plan root is not an object")
    return plan


async def _explain_revision_query() -> dict[str, Any]:
    row = await ContractDatabase.fetch_one(
        """
        EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
        SELECT
            chunk_id,
            section_id,
            chunk_type,
            content,
            sort_order,
            source_chunk_path,
            file_path,
            chunk_metadata,
            id
        FROM document_chunks
        WHERE document_id = 'doc_lg_1'
          AND job_result_id = 'result_lg_1'
        ORDER BY sort_order, chunk_id, id
        LIMIT 2000
        """
    )
    if row is None:
        raise AssertionError("revision EXPLAIN returned no plan")
    plan_value = next(iter(row.values()))
    if not isinstance(plan_value, list) or not plan_value:
        raise AssertionError("revision EXPLAIN returned an unexpected plan shape")
    plan = plan_value[0]
    if not isinstance(plan, dict):
        raise AssertionError("revision EXPLAIN plan root is not an object")
    return plan


async def _measure_seeded_payload(namespace: str) -> int:
    row = await ContractDatabase.fetch_one(
        """
        SELECT COALESCE(
            SUM(pg_column_size(content) + pg_column_size(chunk_metadata)), 0
        ) AS payload_bytes
        FROM document_chunks
        WHERE user_id = :user_id AND namespace = :namespace
        """,
        {"user_id": _USER_ID, "namespace": namespace},
    )
    if row is None or not isinstance(row.get("payload_bytes"), int):
        raise AssertionError("payload measurement returned an unexpected value")
    return row["payload_bytes"]


def _plan_nodes(plan_node: dict[str, Any]) -> list[str]:
    summary = (
        f"{plan_node.get('Node Type')}"
        f"[{plan_node.get('Relation Name', '')}"
        f"/{plan_node.get('Index Name', '')}]"
        f"={plan_node.get('Actual Total Time')}ms"
    )
    children = plan_node.get("Plans", [])
    if not isinstance(children, list):
        return [summary]
    nodes = [summary]
    for child in children:
        if isinstance(child, dict):
            nodes.extend(_plan_nodes(child))
    return nodes


async def test_large_snapshot_keeps_all_retrieval_inputs_after_sql_optimization(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    namespace = f"large-corpus-{uuid4().hex[:8]}"
    async with developer_api_client_factory():
        await _seed_large_retrieval_corpus(namespace)
        payload_bytes = await _measure_seeded_payload(namespace)
        print(
            "seeded payload: "
            f"{payload_bytes / (1024 * 1024):.1f}MiB "
            f"content_bytes={_CONTENT_BYTES} "
            f"sections_per_document={_SECTIONS_PER_DOCUMENT}"
        )

        explain_plan = await _explain_snapshot_query(namespace)
        explain_root = explain_plan["Plan"]
        assert isinstance(explain_root, dict)
        print(
            "snapshot EXPLAIN: "
            f"node={explain_root.get('Node Type')} "
            f"time={explain_plan.get('Execution Time')}ms "
            f"shared_hit={explain_root.get('Shared Hit Blocks')} "
            f"shared_read={explain_root.get('Shared Read Blocks')} "
            f"nodes={' -> '.join(_plan_nodes(explain_root))}"
        )
        revision_plan = await _explain_revision_query()
        revision_root = revision_plan["Plan"]
        assert isinstance(revision_root, dict)
        print(
            "revision EXPLAIN: "
            f"time={revision_plan.get('Execution Time')}ms "
            f"nodes={' -> '.join(_plan_nodes(revision_root))}"
        )
        await ContractDatabase.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_benchmark_chunks_revision_order
            ON document_chunks (document_id, job_result_id, sort_order, chunk_id, id)
            """
        )
        indexed_explain_plan = await _explain_snapshot_query(namespace)
        indexed_root = indexed_explain_plan["Plan"]
        assert isinstance(indexed_root, dict)
        print(
            "snapshot EXPLAIN with candidate index: "
            f"node={indexed_root.get('Node Type')} "
            f"time={indexed_explain_plan.get('Execution Time')}ms "
            f"nodes={' -> '.join(_plan_nodes(indexed_root))}"
        )
        indexed_revision_plan = await _explain_revision_query()
        indexed_revision_root = indexed_revision_plan["Plan"]
        assert isinstance(indexed_revision_root, dict)
        print(
            "revision EXPLAIN with candidate index: "
            f"time={indexed_revision_plan.get('Execution Time')}ms "
            f"nodes={' -> '.join(_plan_nodes(indexed_revision_root))}"
        )
        await ContractDatabase.execute(
            "DROP INDEX IF EXISTS idx_benchmark_chunks_revision_order"
        )

        legacy_started = time.perf_counter()
        legacy_rows = await _load_legacy_rows(namespace)
        legacy_elapsed = time.perf_counter() - legacy_started

        optimized_started = time.perf_counter()
        async with _contract_db_session() as db:
            snapshot = await load_nav_snapshot(
                db,
                user_id=_USER_ID,
                namespace=namespace,
            )
        optimized_elapsed = time.perf_counter() - optimized_started

        with pytest.raises(DBAPIError, match="statement timeout"):
            await _load_legacy_rows(namespace, statement_timeout_ms=100)

        async with _contract_db_session() as db:
            await db.execute(text("SET LOCAL statement_timeout = 5000"))
            bounded_snapshot = await load_nav_snapshot(
                db,
                user_id=_USER_ID,
                namespace=namespace,
            )

    assert len(legacy_rows) == _TOTAL_CHUNKS
    assert len(snapshot.document_ids) == _DOCUMENT_COUNT
    assert len(bounded_snapshot.document_ids) == _DOCUMENT_COUNT

    optimized_chunks = {
        chunk_id: (document_id, chunk)
        for document_id in snapshot.document_ids
        for section_id in snapshot.provider.children(document_id)
        for chunk in snapshot.provider.self_units(section_id)
        for chunk_id in [chunk.chunk_id]
    }
    assert len(optimized_chunks) == _TOTAL_CHUNKS

    legacy_by_chunk = {str(row[1]): row for row in legacy_rows}
    for chunk_id, (document_id, chunk) in optimized_chunks.items():
        legacy_row = legacy_by_chunk[chunk_id]
        assert document_id == str(legacy_row[0])
        assert chunk.section_id == str(legacy_row[2])
        assert chunk.chunk_type == str(legacy_row[3])
        assert chunk.content == str(legacy_row[4])
        assert chunk.sort_order == int(legacy_row[5])
        assert chunk.source_chunk_path == str(legacy_row[6])
        assert chunk.file_path == str(legacy_row[7] or "")
        assert chunk.metadata == (
            legacy_row[8] if isinstance(legacy_row[8], dict) else {}
        )

        reference = snapshot.chunk_ref_index[f"{document_id}:{chunk_id}"]
        assert reference["document_id"] == document_id
        assert reference["section_path"] == str(legacy_row[9] or "")
        assert reference["chunk_type"] == str(legacy_row[3])
        assert reference["file_path"] == (str(legacy_row[7]) if legacy_row[7] else None)
        assert reference["job_id"] == str(legacy_row[10])

    print(
        f"large snapshot benchmark: legacy={legacy_elapsed:.3f}s "
        f"optimized={optimized_elapsed:.3f}s chunks={_TOTAL_CHUNKS}"
    )
