from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import cast
from uuid import uuid4

from httpx import AsyncClient
import pytest
from sqlalchemy import Executable, Result
from sqlalchemy.exc import SQLAlchemyError

from shared.services.retrieval.execution.reference_resolver import (
    resolve_workflow_references,
)
from shared.services.retrieval.nav_snapshot import SnapshotSession, load_nav_snapshot
from shared.services.retrieval.nav_snapshot import _resolve_namespace_snapshot_entries
from tests.support.retrieval_snapshot_support import contract_db_session
from tests.support.contract_database import ContractDatabase


_USER_ID = "local-dev-user"


class _GenerationUnavailableSession:
    def __init__(self) -> None:
        self.rollback_count = 0

    async def execute(self, _statement: Executable) -> Result[tuple[object, ...]]:
        raise SQLAlchemyError("generation table unavailable")

    async def rollback(self) -> None:
        self.rollback_count += 1


@pytest.mark.asyncio
async def test_snapshot_loader_falls_back_when_generation_cannot_be_verified() -> None:
    session = _GenerationUnavailableSession()

    result = await _resolve_namespace_snapshot_entries(
        session,
        user_id=_USER_ID,
        namespace="default",
        document_revisions=[("doc-a", "result-a")],
    )

    assert result is None
    assert session.rollback_count == 1


class _PublishingSession:
    def __init__(
        self,
        session: SnapshotSession,
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
        {"document_id": document_id, "user_id": _USER_ID, "namespace": namespace},
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

        async with contract_db_session() as db:
            snapshot = await load_nav_snapshot(
                _PublishingSession(db, publish_new_revision),
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


@pytest.mark.asyncio
async def test_reference_hydration_keeps_the_captured_revision_after_republish(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    namespace = f"revision-hydration-race-{uuid4().hex[:8]}"
    async with developer_api_client_factory():
        document_id, new_result_id = await _seed_republished_document(namespace)
        revision_rows = await ContractDatabase.fetch_all(
            """
            SELECT job_result_id, chunk_id
            FROM document_chunks
            WHERE document_id = :document_id
            ORDER BY job_result_id
            """,
            {"document_id": document_id},
        )
        old_revision = next(
            row for row in revision_rows if row["job_result_id"] != new_result_id
        )
        await ContractDatabase.execute(
            """
            UPDATE documents
            SET current_job_result_id = :new_result_id
            WHERE document_id = :document_id
            """,
            {"new_result_id": new_result_id, "document_id": document_id},
        )

        async with contract_db_session() as db:
            resolved = await resolve_workflow_references(
                db=db,
                user_id=_USER_ID,
                namespace=namespace,
                refs=[
                    {
                        "document_id": document_id,
                        "chunk_id": old_revision["chunk_id"],
                    }
                ],
                revision_pins={document_id: old_revision["job_result_id"]},
            )

    assert [row["content"] for row in resolved.rows] == ["old content"]
    assert [row["job_result_id"] for row in resolved.rows] == [
        old_revision["job_result_id"]
    ]
