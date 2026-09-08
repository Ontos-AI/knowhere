from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import cast
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import Executable, Result
from sqlalchemy.sql.selectable import Select

from shared.services.retrieval.nav_snapshot import SnapshotSession, load_nav_snapshot
from tests.support.retrieval_snapshot_support import contract_db_session
from tests.support.contract_database import ContractDatabase


_USER_ID = "local-dev-user"


class _CountingSession:
    def __init__(self, session: SnapshotSession) -> None:
        self._session = session
        self.chunk_query_count = 0

    async def execute(self, statement: Executable) -> Result[tuple[object, ...]]:
        selected_tables = (
            {
                getattr(table, "name", "")
                for table in statement.get_final_froms()
            }
            if isinstance(statement, Select)
            else set()
        )
        if "document_chunks" in selected_tables:
            self.chunk_query_count += 1
        result = await self._session.execute(statement)
        return cast(Result[tuple[object, ...]], result)


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


async def test_snapshot_batches_chunks_across_many_small_documents(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    document_count = 25
    namespace = f"batch-documents-{uuid4().hex[:8]}"
    async with developer_api_client_factory():
        await _seed_many_small_documents(namespace, document_count=document_count)
        async with contract_db_session() as db:
            counting_db = _CountingSession(db)
            snapshot = await load_nav_snapshot(
                counting_db,
                user_id=_USER_ID,
                namespace=namespace,
            )

    assert len(snapshot.document_ids) == document_count
    assert counting_db.chunk_query_count == 1
