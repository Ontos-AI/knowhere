"""Archive and a progressive writer must acquire lifecycle locks consistently."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shared.models.database.document import Document
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.publication_scope_content import (
    replace_document_scope_content,
)
from shared.services.retrieval.publication_service import RetrievalPublicationService
from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)
from tests.support.publication_benchmark_support import ensure_benchmark_import_path
from tests.support.retrieval_snapshot_support import contract_db_session

ensure_benchmark_import_path()

from scripts.publication_benchmark.publication_execution import (  # noqa: E402
    PublicationScope,
    seed_publication_fixture,
)


def test_archive_waits_for_progressive_writer_without_a_lock_cycle(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    from app.services.documents import lifecycle_service

    async def ignore_cache_invalidation(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        lifecycle_service,
        "invalidate_retrieval_cache_namespaces",
        ignore_cache_invalidation,
    )
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    suffix: str = uuid4().hex[:10]
    application_name: str = f"archive-race-{suffix}"
    scope = PublicationScope(
        user_ref=f"archive-user-{suffix}",
        namespace_ref=f"archive-namespace-{suffix}",
        source_file_name="report.pdf",
        job_ref=f"archive-job-{suffix}",
        revision_ref=f"archive-result-{suffix}",
    )
    chunks: list[dict[str, object]] = [
        {
            "chunk_id": f"page-{suffix}",
            "type": "page",
            "path": "report.pdf/Results",
            "content": "Original page evidence",
            "metadata": {"page_nums": [1], "scope_id": "p1-1"},
        }
    ]
    try:
        with Session(engine) as db:
            seed_publication_fixture(db, scope=scope)
            published = RetrievalPublicationService().publish_document_state(
                db,
                job_id=scope.job_ref,
                job_result_id=scope.revision_ref,
                chunks=chunks,
            )
            assert published is not None and published.document_id is not None
            document_id: str = published.document_id
            db.commit()

        async def archive() -> dict[str, object] | None:
            async with contract_db_session() as db:
                await db.execute(
                    text("SELECT set_config('application_name', :name, false)"),
                    {"name": application_name},
                )
                await db.execute(text("SET LOCAL statement_timeout = '8s'"))
                return await lifecycle_service.DocumentService().archive_document(
                    db, user_id=scope.user_ref, document_id=document_id
                )

        with ThreadPoolExecutor(max_workers=1) as worker, Session(engine) as writer:
            writer.execute(text("SET LOCAL statement_timeout = '8s'"))
            writer.execute(
                select(Document)
                .where(Document.document_id == document_id)
                .with_for_update()
            ).scalar_one()
            archived = worker.submit(lambda: asyncio.run(archive()))
            deadline: float = time.monotonic() + 5
            while time.monotonic() < deadline:
                with engine.connect() as observer:
                    is_waiting: bool = bool(
                        observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE application_name = :name "
                                "AND wait_event_type = 'Lock')"
                            ),
                            {"name": application_name},
                        )
                    )
                if is_waiting:
                    break
                assert not archived.done(), "Archive must wait for the document writer."
                time.sleep(0.01)
            else:
                pytest.fail("The archive did not reach its contended lifecycle lock.")

            replace_document_scope_content(
                writer,
                scope=DocumentPublicationScope(
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    document_id=document_id,
                    job_result_id=scope.revision_ref,
                    source_file_name="report.pdf",
                ),
                scope_id="p1-1",
                chunks=chunks,
            )
            writer.commit()
            outcome: dict[str, object] | None = archived.result(timeout=10)
            assert outcome is not None and outcome["status"] == "archived"

        with Session(engine) as db:
            document: Document = db.execute(
                select(Document).where(Document.document_id == document_id)
            ).scalar_one()
            assert document.status == "archived"
            assert document.archived_at is not None
    finally:
        engine.dispose()
