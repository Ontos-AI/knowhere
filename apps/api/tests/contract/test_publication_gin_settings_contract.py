"""PostgreSQL contract for candidate-only, transaction-local GIN tuning."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from shared.core.config import settings
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


def _scope(strategy: str, owner: str) -> PublicationScope:
    suffix = f"{strategy}-{owner}"
    return PublicationScope(
        user_ref=f"bench-gin-user-{suffix}",
        namespace_ref=f"bench-gin-namespace-{suffix}",
        source_file_name=f"bench-gin-{suffix}.pdf",
        job_ref=f"bench-gin-job-{suffix}",
        revision_ref=f"bench-gin-result-{suffix}",
    )


def _publish(db: Session, scope: PublicationScope) -> str:
    published = RetrievalPublicationService().publish_document_state(
        db,
        job_id=scope.job_ref,
        job_result_id=scope.revision_ref,
        chunks=[
            {
                "chunk_id": "gin-contract-chunk",
                "type": "text",
                "content": "SpaceX publication contract evidence",
                "path": f"{scope.source_file_name}/Root/Body",
                "metadata": {},
            }
        ],
        update_namespace_snapshot=False,
    )
    assert published is not None and published.document_id is not None
    return str(db.execute(text("SHOW gin_pending_list_limit")).scalar_one())


@pytest.mark.parametrize("strategy", ("baseline", "candidate"))
def test_gin_limit_is_local_to_sync_publication_transaction(
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
    scope = _scope(strategy, "sync")
    try:
        with Session(engine) as session:
            seed_publication_fixture(session, scope=scope)
            session.commit()
            default_limit = str(session.execute(text("SHOW gin_pending_list_limit")).scalar_one())
            observed_limit = _publish(session, scope)
            assert observed_limit == ("64MB" if strategy == "candidate" else default_limit)
            session.rollback()
            assert str(session.execute(text("SHOW gin_pending_list_limit")).scalar_one()) == default_limit
    finally:
        engine.dispose()


@pytest.mark.parametrize("strategy", ("baseline", "candidate"))
async def test_gin_limit_is_local_to_async_publication_transaction(
    strategy: str,
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    await prepare_contract_storage()
    monkeypatch.setattr(settings, "KNOWHERE_PUBLICATION_STRATEGY", strategy)
    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+asyncpg"
    )
    engine = create_async_engine(database_url)
    scope = _scope(strategy, "async")
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            await session.run_sync(lambda db: seed_publication_fixture(db, scope=scope))
            await session.commit()
            default_limit = str((await session.execute(text("SHOW gin_pending_list_limit"))).scalar_one())
            observed_limit = await session.run_sync(lambda db: _publish(db, scope))
            assert observed_limit == ("64MB" if strategy == "candidate" else default_limit)
            await session.rollback()
            assert str((await session.execute(text("SHOW gin_pending_list_limit"))).scalar_one()) == default_limit
    finally:
        await engine.dispose()
