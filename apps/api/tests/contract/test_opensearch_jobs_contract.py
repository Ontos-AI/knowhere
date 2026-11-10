"""Original-request indexing retries retain results and reject active attempts."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from httpx import AsyncClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from shared.testing.contract_runtime import get_contract_database_url


@pytest.mark.asyncio
@pytest.mark.parametrize("file_name", ["report.pdf", "report.md"])
async def test_native_admission_accepts_progressive_publication(
    monkeypatch: pytest.MonkeyPatch,
    developer_api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    file_name: str,
) -> None:
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_URL", "http://127.0.0.1:1")
    async with developer_api_client_factory() as client:
        response = await client.post("/api/v2/jobs", json={
            "source_type": "file", "file_name": file_name, "ingestion_mode": "progressive",
        })
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "waiting-file"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["v1", "v2"])
async def test_indexing_retry_preserves_results_and_rejects_active_attempts(
    monkeypatch: pytest.MonkeyPatch,
    developer_api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    version: str,
) -> None:
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_URL", "http://127.0.0.1:1")
    async with developer_api_client_factory() as client:
        created = await client.post(f"/api/{version}/jobs", json={"source_type": "file", "file_name": "report.pdf"})
        assert created.status_code == 200, created.text
        jobId: str = created.json()["job_id"]
        from shared.core.celery_app import celery_app
        from shared.models.database.indexing import IndexingAttempt
        from shared.models.database.job import Job
        from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
        from shared.services.retrieval.lexical.retry import IndexingRetry
        from shared.services.retrieval.lexical.worker import IndexingWorker
        monkeypatch.setattr(celery_app, "send_task", lambda *args, **kwargs: None)
        engine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))
        try:
            with Session(engine) as session:
                job = session.get(Job, jobId)
                assert job is not None
                job.status = "running"
                session.flush()
                SyncJobSuccessFinalizer().finalize(session, job_id=jobId,
                    result_s3_key="results/parsed.zip", checksum="checksum", zip_size=100,
                    chunks=[{"chunk_id": "retry-chunk", "type": "text", "content": "工程验收", "path": "report.pdf/验收", "metadata": {}}],
                    stored_count=1, delivery_mode="url", section_summaries=None)
                session.commit()
            parsed = await client.get(f"/api/{version}/jobs/{jobId}")
            assert parsed.status_code == 200
            assert parsed.json()["parsing_status"] == "completed"
            assert parsed.json()["indexing_status"] == "pending"
            assert parsed.json()["result_url"]
            worker = IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False))
            assert worker.run(jobId) == "failed"
            assert worker.run(jobId) == "failed"  # Redelivery cannot execute it again.
            with Session(engine) as session:
                attempt: IndexingAttempt = session.scalars(select(IndexingAttempt).where(IndexingAttempt.jobId == jobId)).one()
                assert attempt.executionCount == 1 and attempt.state == "failed"
            failed = await client.get(f"/api/{version}/jobs/{jobId}")
            assert failed.json()["failure_stage"] == "indexing"
            assert failed.json()["result_url"] == parsed.json()["result_url"]
            retried = await client.post(f"/api/{version}/jobs/{jobId}/indexing/retry")
            assert retried.status_code == 503, retried.text
            assert retried.json()["error"]["code"] == "UNAVAILABLE"
            details = retried.json()["error"]["details"]
            assert details["job_id"] == jobId
            assert details["parsing_status"] == "completed" and details["indexing_status"] == "failed"
            with Session(engine) as session:
                attempts: list[IndexingAttempt] = list(session.scalars(select(IndexingAttempt).where(
                    IndexingAttempt.jobId == jobId).order_by(IndexingAttempt.attemptNumber)))
                assert [attempt.executionCount for attempt in attempts] == [1, 1]
                assert [attempt.state for attempt in attempts] == ["failed", "failed"]
                job = session.get(Job, jobId)
                assert job is not None
                assert IndexingRetry().request(session, jobId=jobId, userId=str(job.user_id))
                session.commit()
            repeated = await client.post(f"/api/{version}/jobs/{jobId}/indexing/retry")
            assert repeated.status_code == 409, repeated.text
            assert repeated.json()["error"]["details"]["reason"] == "INDEXING_ALREADY_RUNNING"
            with Session(engine) as session:
                attempts = list(session.scalars(select(IndexingAttempt).where(IndexingAttempt.jobId == jobId)
                    .order_by(IndexingAttempt.attemptNumber)))
                assert len(attempts) == 3 and attempts[-1].executionCount == 0
        finally:
            engine.dispose()
