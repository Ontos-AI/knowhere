"""Retry admission uses the active publication and never reopens ingestion."""

import asyncio

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from shared.models.database.document import Document, DocumentScopeEnrichment
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.testing.contract_runtime import (
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


def test_retry_is_authorized_fenced_and_idempotent(
    monkeypatch, postgresql_proc
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    from app.services.jobs.enrichment_retry_service import retry_job_enrichment
    from shared.core.exceptions.domain_exceptions import (
        ConflictException,
        NotFoundException,
    )
    from shared.services.jobs.lifecycle import scope_enrichments

    sent: list[str] = []
    monkeypatch.setattr(
        scope_enrichments,
        "enqueue_progressive_recovery_after_commit",
        lambda revision: sent.append(revision),
    )
    scope = PublicationScope(
        user_ref="retry-user",
        namespace_ref="retry",
        source_file_name="x.pdf",
        job_ref="retry-job",
        revision_ref="retry-result",
    )
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    with Session(engine) as db:
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM pg_indexes WHERE tablename = 'job_results' AND indexname = 'idx_job_results_pending_enhancement'"
                )
            )
            == 1
        )
        seed_publication_fixture(db, scope=scope)
        document = Document(
            document_id="retry-doc",
            user_id=scope.user_ref,
            namespace=scope.namespace_ref,
            current_job_result_id=scope.revision_ref,
        )
        db.add(document)
        db.flush()
        result = db.get(JobResult, scope.revision_ref)
        assert result is not None
        result.document_id = document.document_id
        result.document_metadata = {
            "progressive": {
                "enhancement": {"status": "failed", "can_retry": True, "attempt": 1},
                "stages": {"structure": {"status": "complete", "attempts": 1}},
                "snapshots": [
                    {
                        "snapshot_id": "retry-baseline",
                        "stage": "baseline",
                        "job_id": scope.job_ref,
                        "job_result_id": scope.revision_ref,
                        "document_id": document.document_id,
                        "checksum": "a" * 64,
                        "archive_key": "results/retry-job/snapshots/retry-baseline.zip",
                    }
                ],
                "current_snapshot_id": "retry-baseline",
            }
        }
        job = db.get(Job, scope.job_ref)
        assert job is not None
        job.status = "done"
        job.page_count = 4
        job.job_metadata = {**(job.job_metadata or {}), "page_count": 4}
        job.credits_charged = 123
        job.billing_status = "charged"
        db.add_all(
            [
                DocumentScopeEnrichment(
                    document_id="retry-doc",
                    job_result_id=scope.revision_ref,
                    scope_id="p1-2",
                    page_start=1,
                    page_end=2,
                    state="failed",
                    attempts=3,
                    claim_generation="old",
                ),
                DocumentScopeEnrichment(
                    document_id="retry-doc",
                    job_result_id=scope.revision_ref,
                    scope_id="p3-4",
                    page_start=3,
                    page_end=4,
                    state="refined",
                    attempts=1,
                    claim_generation="kept",
                ),
            ]
        )
        db.commit()

    async def run_retry() -> None:
        async_engine = create_async_engine(get_contract_database_url())
        try:
            async with AsyncSession(async_engine) as db:
                with pytest.raises(NotFoundException):
                    await retry_job_enrichment(
                        db, user_id="other-user", job_id=scope.job_ref
                    )
                await db.rollback()
                first = await retry_job_enrichment(
                    db, user_id=scope.user_ref, job_id=scope.job_ref
                )
                second = await retry_job_enrichment(
                    db, user_id=scope.user_ref, job_id=scope.job_ref
                )
                assert first.enhancement.attempt == second.enhancement.attempt == 2
                from app.services.jobs.read_service import get_job_result_for_user

                projection = await get_job_result_for_user(
                    db, user_id=scope.user_ref, job_id=scope.job_ref
                )
                payload = projection.model_dump(mode="json")
                assert payload["page_count"] == 4
                assert payload["enhancement"]["status"] == "pending"
                assert payload["current_snapshot_id"] == "retry-baseline"
                assert payload["snapshots"][0]["result_url"]
                assert "archive_key" not in payload["snapshots"][0]
                document = await db.get(Document, "retry-doc")
                assert document is not None
                document.status = "archived"
                await db.commit()
                with pytest.raises(ConflictException):
                    await retry_job_enrichment(
                        db, user_id=scope.user_ref, job_id=scope.job_ref
                    )
        finally:
            await async_engine.dispose()

    asyncio.run(run_retry())
    with Session(engine) as db:
        job = db.get(Job, scope.job_ref)
        assert job is not None
        assert (job.status, job.credits_charged, job.billing_status) == (
            "done",
            123,
            "charged",
        )
        rows = db.scalars(
            select(DocumentScopeEnrichment)
            .where(DocumentScopeEnrichment.job_result_id == scope.revision_ref)
            .order_by(DocumentScopeEnrichment.scope_id)
        ).all()
        assert [(row.state, row.attempts, row.claim_generation) for row in rows] == [
            ("coarse", 0, None),
            ("refined", 1, "kept"),
        ]
    assert sent
    engine.dispose()
