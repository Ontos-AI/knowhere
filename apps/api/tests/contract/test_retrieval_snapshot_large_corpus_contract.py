from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import cast
from uuid import uuid4

from httpx import AsyncClient
import pytest

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.models.database.job_result import JobResult
from shared.services.retrieval.nav_snapshot import (
    SnapshotSession,
    _CHUNK_BATCH_SIZE,
    _REVISION_GROUP_SIZE,
    load_nav_snapshot,
)
import shared.services.retrieval.nav_snapshot as nav_snapshot_module
from sqlalchemy import Executable, Result, select
from sqlalchemy.engine import Row
from sqlalchemy.sql.selectable import Select
from tests.support.retrieval_snapshot_support import contract_db_session
from tests.support.contract_database import ContractDatabase


_USER_ID = "local-dev-user"
_DOCUMENT_COUNT = 100
_CHUNKS_PER_DOCUMENT = 600
_SECTIONS_PER_DOCUMENT = 8
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


class _CountingSession:
    def __init__(self, session: SnapshotSession) -> None:
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


async def _load_legacy_rows(
    namespace: str,
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
    async with contract_db_session() as db:
        rows: list[LegacySnapshotRow] = list((await db.execute(stmt)).all())
        return rows


async def test_large_snapshot_keeps_all_retrieval_inputs_after_bounded_sql_load(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = f"large-corpus-{uuid4().hex[:8]}"
    async with developer_api_client_factory():
        await _seed_large_retrieval_corpus(namespace)
        async def unexpected_manifest_load(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("large snapshots must use normalized retrieval rows")

        monkeypatch.setattr(
            nav_snapshot_module,
            "_load_manifest_sections",
            unexpected_manifest_load,
        )
        legacy_rows = await _load_legacy_rows(namespace)
        async with contract_db_session() as db:
            counting_db = _CountingSession(db)
            snapshot = await load_nav_snapshot(
                counting_db,
                user_id=_USER_ID,
                namespace=namespace,
            )
        expected_chunk_query_count = sum(
            (
                min(_REVISION_GROUP_SIZE, _DOCUMENT_COUNT - group_start)
                * _CHUNKS_PER_DOCUMENT
                + _CHUNK_BATCH_SIZE
                - 1
            )
            // _CHUNK_BATCH_SIZE
            for group_start in range(0, _DOCUMENT_COUNT, _REVISION_GROUP_SIZE)
        )
        assert counting_db.chunk_query_count == expected_chunk_query_count

        async with contract_db_session() as db:
            bounded_snapshot = await load_nav_snapshot(
                db,
                user_id=_USER_ID,
                namespace=namespace,
            )

    assert len(legacy_rows) == _TOTAL_CHUNKS
    assert len(snapshot.document_ids) == _DOCUMENT_COUNT
    assert len(bounded_snapshot.document_ids) == _DOCUMENT_COUNT

    optimized_rows = [
        (
            document_id,
            chunk.chunk_id,
            chunk.section_id,
            chunk.chunk_type,
            chunk.content,
            chunk.sort_order,
            chunk.source_chunk_path,
            chunk.file_path,
            chunk.metadata,
            snapshot.chunk_ref_index[f"{document_id}:{chunk.chunk_id}"][
                "section_path"
            ],
            snapshot.chunk_ref_index[f"{document_id}:{chunk.chunk_id}"]["job_id"],
        )
        for document_id in snapshot.document_ids
        for section_id in snapshot.provider.children(document_id)
        for chunk in snapshot.provider.self_units(section_id)
    ]
    optimized_rows.sort(key=lambda row: (row[0], row[5], row[1], row[2] or ""))
    legacy_rows_projected = [
        (
            str(row[0]),
            str(row[1]),
            str(row[2]) if row[2] else None,
            str(row[3]),
            str(row[4]),
            int(row[5]),
            str(row[6]),
            str(row[7] or ""),
            row[8] if isinstance(row[8], dict) else {},
            str(row[9] or ""),
            str(row[10]) if row[10] else None,
        )
        for row in legacy_rows
    ]
    def row_order_key(row: tuple[object, ...]) -> tuple[str, ...]:
        return (
            str(row[0]),
            str(row[5]),
            str(row[1]),
            str(row[2] or ""),
            str(row[3]),
            str(row[4]),
            str(row[6]),
            str(row[7] or ""),
            str(row[8]),
            str(row[9] or ""),
            str(row[10] or ""),
        )
    legacy_rows_projected.sort(key=row_order_key)
    optimized_rows.sort(key=row_order_key)
    assert len(optimized_rows) == _TOTAL_CHUNKS
    assert optimized_rows == legacy_rows_projected
