"""A completed publication revision replays as a transaction-safe no-op."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shared.core.config import settings
from shared.models.database.document import Document
from shared.models.database.job_result import JobResult
from shared.services.retrieval.publication_service import RetrievalPublicationService
from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)
from tests.support.publication_benchmark_support import ensure_benchmark_import_path

ensure_benchmark_import_path()

from scripts.publication_benchmark.publication_execution import (  # noqa: E402
    PublicationScope,
    seed_publication_fixture,
)


def _scope(strategy: str) -> PublicationScope:
    suffix = f"replay-{strategy}"
    return PublicationScope(
        user_ref=f"bench-replay-user-{suffix}",
        namespace_ref=f"bench-replay-namespace-{suffix}",
        source_file_name=f"bench-replay-{suffix}.pdf",
        job_ref=f"bench-replay-job-{suffix}",
        revision_ref=f"bench-replay-result-{suffix}",
    )


def _chunks(scope: PublicationScope) -> list[dict[str, object]]:
    return [
        {
            "chunk_id": "replay-contract-chunk",
            "type": "text",
            "content": "A publication revision must be replay safe",
            "path": f"{scope.source_file_name}/Root/Body",
            "metadata": {},
        }
    ]


@pytest.mark.parametrize("strategy", ("baseline", "candidate"))
def test_completed_revision_replay_is_a_no_op(
    strategy: str,
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    monkeypatch.setattr(settings, "KNOWHERE_PUBLICATION_STRATEGY", strategy)
    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    service = RetrievalPublicationService()
    scope = _scope(strategy)
    chunks = _chunks(scope)
    try:
        with Session(engine) as session:
            seed_publication_fixture(session, scope=scope)
            session.commit()
            first = service.publish_document_state(
                session,
                job_id=scope.job_ref,
                job_result_id=scope.revision_ref,
                chunks=chunks,
                update_namespace_snapshot=False,
            )
            assert first is not None
            assert first.document_id is not None
            session.commit()

        with Session(engine) as session:
            replay = service.publish_document_state(
                session,
                job_id=scope.job_ref,
                job_result_id=scope.revision_ref,
                chunks=chunks,
                update_namespace_snapshot=False,
            )
            assert replay is not None
            assert replay.document_id is None
            assert replay.skipped_all_duplicate is True
            session.commit()

        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(Document)) == 1
            bound_document_id = session.scalar(
                select(JobResult.document_id).where(JobResult.id == scope.revision_ref)
            )
            assert bound_document_id == first.document_id
    finally:
        engine.dispose()
