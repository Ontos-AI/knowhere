"""Durable indexing attempts execute once and preserve their parsed revision."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    clear_application_modules,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)


@pytest.fixture
def parsedRuntime(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> Iterator[tuple[Engine, str]]:
    configure_contract_environment(monkeypatch, postgresql_proc)
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_URL", "http://127.0.0.1:9")
    clear_application_modules()
    asyncio.run(prepare_contract_storage())
    from shared.services.webhook import qstash_publisher

    class QuietPublisher:
        def publish_event(self, eventId: str) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(qstash_publisher, "QStashWebhookPublisher", QuietPublisher)
    from shared.models.database.job import Job
    from shared.models.database.user import User
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer

    engine: Engine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))
    jobId: str = f"execution-{uuid4().hex[:12]}"
    with Session(engine) as session:
        session.add(User(id="execution-user", name="Execution contract", email="execution@example.invalid"))
        session.flush()
        session.add(Job(job_id=jobId, user_id="execution-user", job_type="document_ingestion",
            source_type="file", status="running", credits_charged=123456, billing_status="charged",
            webhook_enabled=True, webhook_url="https://example.invalid/callback",
            webhook_events=["job.parsed", "job.completed", "job.failed"],
            job_metadata={"document_id": "doc_execution", "namespace": "execution-contract", "source_file_name": "report.pdf"}))
        session.commit()
        result = SyncJobSuccessFinalizer().finalize(session, job_id=jobId,
            result_s3_key="results/execution.zip", checksum="execution-checksum", zip_size=100,
            chunks=[{"chunk_id": "execution-chunk", "type": "text", "content": "工程验收计划", "path": "report.pdf/工程", "metadata": {}}],
            stored_count=1, delivery_mode="url", section_summaries=None)
        assert result.response.should_commit()
        session.commit()
    try:
        yield engine, jobId
    finally:
        engine.dispose()


def test_native_connection_failure_consumes_one_terminal_execution(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.webhook import WebhookEvent
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.worker import IndexingWorker

    engine, jobId = parsedRuntime
    observations: list[tuple[int, str]] = []

    def failConnection(self: OpenSearchLexicalIndex, *, isExact: bool,
                       timeoutSeconds: float) -> NativeLifecycleSnapshot:
        with Session(engine) as session:
            attempt: IndexingAttempt = session.scalars(select(IndexingAttempt)).one()
            observations.append((attempt.executionCount, attempt.state))
        raise LexicalIndexError("Injected native connection failure", isRetryable=True)

    monkeypatch.setattr(OpenSearchLexicalIndex, "getLifecycleSnapshot", failConnection)
    worker: IndexingWorker = IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False))
    assert worker.run(jobId) == "failed"
    assert observations == [(1, "running")]
    assert worker.run(jobId) == "failed"
    assert observations == [(1, "running")]
    with Session(engine) as session:
        attempt = session.scalars(select(IndexingAttempt)).one()
        job: Job = session.scalars(select(Job)).one()
        assert attempt.executionCount == 1 and attempt.state == "failed"
        assert job.parsing_status == "completed" and job.indexing_status == "failed"
        assert job.status == "failed" and job.failure_stage == "indexing"
        assert job.billing_status == "charged" and job.credits_charged == 123456
        assert session.scalar(select(func.count()).select_from(JobResult)) == 1
        events: list[WebhookEvent] = list(session.scalars(select(WebhookEvent)))
        assert sorted(event.payload["event"] for event in events) == ["job.failed", "job.parsed"]


def test_committed_parsing_runs_inline_without_waiting_for_webhook_transport(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.jobs.lifecycle.post_commit_effects import PostCommitEffectPlan, SyncJobPostCommitEffectRunner
    from shared.services.retrieval.lexical import dispatch
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.webhook import qstash_publisher

    _, jobId = parsedRuntime
    started: Event = Event()
    release: Event = Event()
    finished: Event = Event()
    calls: list[str] = []

    class BlockedPublisher:
        def publish_event(self, eventId: str) -> None:
            started.set()
            release.wait()

        def close(self) -> None:
            finished.set()

    def runInline(self: IndexingWorker, identity: str, **options: object) -> str:
        assert identity == jobId
        calls.append("inline")
        return "failed"

    monkeypatch.setattr(qstash_publisher, "QStashWebhookPublisher", BlockedPublisher)
    monkeypatch.setattr(dispatch, "dispatchIndexing", lambda identity: calls.append("queued"))
    monkeypatch.setattr(IndexingWorker, "run", runInline)
    plan = PostCommitEffectPlan(indexing_job_ids=(jobId,), webhook_event_ids=("event-parsed",))
    with ThreadPoolExecutor(max_workers=1) as caller:
        result = caller.submit(SyncJobPostCommitEffectRunner().run, plan)
        try:
            assert started.wait(timeout=5), "Webhook transport did not start"
            # The caller must return before the test lets the transport finish.
            result.result(timeout=5)
            assert calls == ["inline"]
        finally:
            release.set()
    assert finished.wait(timeout=5), "Webhook transport did not finish after release"


def test_execution_deadline_is_persisted_before_native_admission(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime
    from shared.models.database.indexing import IndexingAttempt
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.worker import IndexingWorker

    engine, jobId = parsedRuntime
    observations: list[tuple[datetime | None, datetime | None]] = []

    def inspectDeadline(self: OpenSearchLexicalIndex, *, isExact: bool,
                        timeoutSeconds: float) -> NativeLifecycleSnapshot:
        with Session(engine) as session:
            attempt: IndexingAttempt = session.scalars(select(IndexingAttempt)).one()
            observations.append((getattr(attempt, "startedAt", None), getattr(attempt, "executionDeadlineAt", None)))
        raise LexicalIndexError("Stop after observing the committed deadline")

    monkeypatch.setattr(OpenSearchLexicalIndex, "getLifecycleSnapshot", inspectDeadline)
    assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId) == "failed"
    assert len(observations) == 1
    startedAt, deadlineAt = observations[0]
    assert startedAt is not None and deadlineAt is not None
    assert (deadlineAt - startedAt).total_seconds() == pytest.approx(10, abs=0.01)


def test_manual_attempt_gets_a_fresh_admission_deadline_for_old_retained_parse(
    parsedRuntime: tuple[Engine, str],
) -> None:
    from datetime import timedelta
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.retry import IndexingRetry

    engine, jobId = parsedRuntime
    with Session(engine) as session:
        job: Job = session.scalars(select(Job)).one()
        attempt: IndexingAttempt = session.scalars(select(IndexingAttempt)).one()
        now = LexicalCoordination.getDatabaseTime(session)
        attempt.state = "failed"
        attempt.createdAt = now - timedelta(days=30)
        job.status = "failed"
        job.indexing_status = "failed"
        job.failure_stage = "indexing"
        session.commit()
        assert IndexingRetry().request(session, jobId=jobId, userId="execution-user")
        session.commit()
        latest: IndexingAttempt = session.scalars(select(IndexingAttempt).where(IndexingAttempt.attemptNumber == 2)).one()
        deadline = getattr(latest, "admissionDeadlineAt", None)
        assert getattr(latest, "origin", None) == "manual"
        assert deadline is not None and 8 <= (deadline - now).total_seconds() <= 11
        assert latest.executionCount == 0 and getattr(latest, "startedAt", None) is None


def test_execution_timeout_stops_native_wait_and_returns_without_replay(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gevent
    from shared.core.config import settings
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.worker import IndexingWorker

    engine, jobId = parsedRuntime
    calls: list[str] = []
    executionBudget: float = float(os.environ.get("KNOWHERE_TEST_SHORT_INDEXING_TIMEOUT_SECONDS", "0.08"))
    from shared.services.retrieval.lexical.runtime import OpenSearchRuntime
    from types import SimpleNamespace
    # The RPC is fake; omit unrelated cold TLS/client initialization so this
    # check measures cancellation of the blocked native wait itself.
    monkeypatch.setattr(OpenSearchRuntime, "resolve", lambda endpoint: SimpleNamespace(client=None, release=lambda: None))

    def blockedNative(self: OpenSearchLexicalIndex, *, isExact: bool,
                      timeoutSeconds: float) -> NativeLifecycleSnapshot:
        calls.append("native")
        gevent.sleep(executionBudget * 20)
        raise LexicalIndexError("Native wait reached its deliberately long end")

    monkeypatch.setattr(settings, "KNOWHERE_INDEXING_TIMEOUT_SECONDS", executionBudget)
    monkeypatch.setattr(OpenSearchLexicalIndex, "getLifecycleSnapshot", blockedNative)
    startedAt: float = time.monotonic()
    worker: IndexingWorker = IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False))
    assert worker.run(jobId) == "failed"
    assert time.monotonic() - startedAt < executionBudget + 0.32 + (
        settings.KNOWHERE_INDEXING_SETTLEMENT_TIMEOUT_SECONDS if executionBudget > 0.08 else 0)
    assert calls == ["native"]
    with Session(engine) as session:
        attempt: IndexingAttempt = session.scalars(select(IndexingAttempt)).one()
        job: Job = session.scalars(select(Job)).one()
        assert attempt.executionCount == 1 and attempt.state == "failed"
        assert job.parsing_status == "completed" and job.failure_stage == "indexing"
        assert job.error_code == "DEADLINE_EXCEEDED"


def _createUnparsedJob(engine: Engine) -> str:
    from shared.models.database.document import Document
    from shared.models.database.job import Job

    jobId: str = f"parse-owner-{uuid4().hex[:12]}"
    with Session(engine) as session:
        session.add(Job(job_id=jobId, user_id="execution-user", job_type="document_ingestion",
            source_type="file", status="running", parsing_status="running",
            job_metadata={"document_id": "doc_parse_owner", "namespace": "parse-owner", "source_file_name": "owner.pdf"}))
        session.flush()
        session.add(Document(document_id="doc_parse_owner", user_id="execution-user", namespace="parse-owner",
            source_file_name="owner.pdf", status="active", accepted_job_id=jobId))
        session.commit()
    return jobId


def test_parse_owner_takeover_fences_the_paused_old_owner(
    parsedRuntime: tuple[Engine, str],
) -> None:
    from datetime import timedelta
    from shared.models.database.job import Job
    from shared.services.jobs.parsing import ParsingOwnership
    from shared.services.retrieval.lexical.coordination import LexicalCoordination

    engine, _ = parsedRuntime
    jobId: str = _createUnparsedJob(engine)
    ownership: ParsingOwnership = ParsingOwnership(sessionFactory=sessionmaker(engine, expire_on_commit=False))
    assert ownership.claim(jobId, "old-owner")
    ownership.assertOwned(jobId, "old-owner")
    assert not ownership.claim(jobId, "another-owner")
    with Session(engine) as session:
        job: Job = session.get(Job, jobId)
        assert job is not None
        job.parsing_owner_expires_at = LexicalCoordination.getDatabaseTime(session) - timedelta(seconds=1)
        session.commit()
    assert ownership.claim(jobId, "new-owner")
    with pytest.raises(RuntimeError, match="ownership"):
        ownership.assertOwned(jobId, "old-owner")
    ownership.assertOwned(jobId, "new-owner")
    assert not ownership.renew(jobId, "old-owner")
    assert ownership.renew(jobId, "new-owner")


def test_stale_parse_owner_cannot_bind_a_result_after_takeover(
    parsedRuntime: tuple[Engine, str],
) -> None:
    from datetime import timedelta
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
    from shared.services.retrieval.lexical.coordination import LexicalCoordination

    engine, _ = parsedRuntime
    jobId: str = _createUnparsedJob(engine)
    with Session(engine) as session:
        job: Job = session.get(Job, jobId)
        assert job is not None
        job.parsing_owner_token = "new-owner"
        job.parsing_owner_expires_at = LexicalCoordination.getDatabaseTime(session) + timedelta(seconds=60)
        session.commit()
        with pytest.raises(RuntimeError, match="ownership"):
            SyncJobSuccessFinalizer().finalize(session, job_id=jobId,
                result_s3_key="results/old-owner.zip", checksum="old-owner", zip_size=100,
                chunks=[], stored_count=0, delivery_mode="url", section_summaries=None,
                parsingOwnerToken="old-owner")
        session.rollback()
        assert session.scalar(select(func.count()).select_from(JobResult).where(JobResult.job_id == jobId)) == 0
        assert session.get(Job, jobId).parsing_owner_token == "new-owner"


def test_worker_lifecycle_passes_the_parsing_owner_fence_to_finalization(
    parsedRuntime: tuple[Engine, str],
) -> None:
    from datetime import timedelta
    from shared.models.database.job import Job
    from shared.services.jobs.lifecycle.service import SyncJobLifecycleService
    from shared.services.retrieval.lexical.coordination import LexicalCoordination

    engine, _ = parsedRuntime
    jobId: str = _createUnparsedJob(engine)
    with Session(engine) as session:
        job = session.get(Job, jobId)
        assert job is not None
        job.parsing_owner_token = "new-owner"
        job.parsing_owner_expires_at = LexicalCoordination.getDatabaseTime(session) + timedelta(seconds=60)
        session.commit()
    with pytest.raises(RuntimeError, match="ownership"):
        SyncJobLifecycleService().finalize_job_success(job_id=jobId,
            result_s3_key="results/stale.zip", checksum="stale", zip_size=1,
            chunks=[], parsingOwnerToken="old-owner")


def test_obsolete_automatic_indexing_delivery_is_not_scheduled(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core.celery_app import celery_app
    from shared.services.retrieval.lexical import tasks
    from shared.services.retrieval.lexical.dispatch import dispatchIndexing
    from shared.services.retrieval.lexical.worker import IndexingWorker

    def rejectExecution(*arguments: object, **options: object) -> str:
        pytest.fail("An obsolete indexing message started an execution")

    monkeypatch.setattr(IndexingWorker, "run", rejectExecution)
    sent: list[str] = []
    monkeypatch.setattr(celery_app, "send_task", lambda name, **options: sent.append(name))
    assert all(entry["task"] != "shared.services.retrieval.lexical.reconcile.reconcileIndexing"
        for entry in celery_app.conf.beat_schedule.values())
    assert tasks.runIndexing.run("obsolete-job") == "skipped"
    engine, jobId = parsedRuntime
    assert not dispatchIndexing(jobId, sessionFactory=sessionmaker(engine, expire_on_commit=False))
    assert sent == []


def test_indexing_database_read_failure_does_not_escape_to_parser_retry(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval.lexical.worker import IndexingWorker
    engine, jobId = parsedRuntime
    worker: IndexingWorker = IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False))

    def failRead(identity: str) -> None:
        raise RuntimeError("Injected unavailable database before execution claim")

    monkeypatch.setattr(worker, "_readContext", failRead)
    assert worker.run(jobId) == "pending"


def test_expired_started_attempt_is_settled_without_native_reexecution(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.worker import IndexingWorker

    engine, jobId = parsedRuntime
    with Session(engine) as session:
        attempt = session.scalars(select(IndexingAttempt)).one()
        now = LexicalCoordination.getDatabaseTime(session)
        attempt.state = "running"
        attempt.executionCount = 1
        attempt.ownerToken = "interrupted-owner"
        attempt.startedAt = now - timedelta(seconds=30)
        attempt.executionDeadlineAt = now - timedelta(seconds=20)
        attempt.claimExpiresAt = attempt.executionDeadlineAt
        session.commit()

    def rejectNative(*arguments: object, **options: object) -> None:
        pytest.fail("An expired execution contacted native storage again")

    monkeypatch.setattr(OpenSearchLexicalIndex, "getLifecycleSnapshot", rejectNative)
    assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId) == "failed"
    with Session(engine) as session:
        attempt = session.scalars(select(IndexingAttempt)).one()
        job = session.get(Job, jobId)
        assert job is not None and job.status == "failed" and job.parsing_status == "completed"
        assert attempt.executionCount == 1 and attempt.state == "failed"


def test_parsing_can_commit_while_primary_publication_is_in_maintenance(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.worker import IndexingWorker

    engine, _ = parsedRuntime
    jobId: str = _createUnparsedJob(engine)
    with Session(engine) as session:
        control = session.get(LexicalBackendControl, 1)
        assert control is not None
        control.mode = "maintenance"
        session.commit()
        finalization = SyncJobSuccessFinalizer().finalize(session, job_id=jobId,
            result_s3_key="results/during-maintenance.zip", checksum="maintenance", zip_size=1,
            chunks=[], stored_count=0, delivery_mode="url", section_summaries=None)
        assert finalization.response.should_commit()
        session.commit()

    def rejectNative(*arguments: object, **options: object) -> None:
        pytest.fail("Maintenance interruption attempted native indexing")

    monkeypatch.setattr(OpenSearchLexicalIndex, "getLifecycleSnapshot", rejectNative)
    assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId) == "failed"
    with Session(engine) as session:
        job = session.get(Job, jobId)
        assert job is not None and job.parsing_status == "completed" and job.failure_stage == "indexing"
        assert session.scalar(select(func.count()).select_from(JobResult).where(JobResult.job_id == jobId)) == 1
        assert session.scalar(select(IndexingAttempt.executionCount).where(IndexingAttempt.jobId == jobId)) == 0


def test_new_input_uploads_have_distinct_immutable_keys(
    parsedRuntime: tuple[Engine, str],
) -> None:
    from shared.services.retrieval.lexical.input import LexicalInput
    from shared.services.retrieval.lexical.storage import LexicalInputStorage

    payload: LexicalInput = LexicalInput(compilerFingerprint="frozen", units=())
    storage: LexicalInputStorage = LexicalInputStorage()
    firstKey, firstChecksum = storage.save(jobId="input-job", revisionId="input-revision", payload=payload)
    secondKey, secondChecksum = storage.save(jobId="input-job", revisionId="input-revision", payload=payload)
    assert firstChecksum == secondChecksum
    assert firstKey != secondKey
    assert storage.load(key=firstKey, checksum=firstChecksum) == payload


@pytest.mark.asyncio
async def test_async_retained_input_uses_async_http_without_sync_transfer_or_executor(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    from shared.services.retrieval.lexical import storage as storageModule
    from shared.core.config import settings
    from shared.services.storage.async_presigner import AsyncStoragePresigner
    from shared.services.retrieval.lexical.deadline import IndexingDeadline
    from shared.services.retrieval.lexical.input import LexicalInput

    stored: dict[str, bytes] = {}
    calls: list[str] = []
    clientType = httpx.AsyncClient

    async def presign(self: AsyncStoragePresigner, key: str, **options: object) -> str:
        await asyncio.sleep(0)
        return f"https://storage.example.invalid/{key}"

    def rejectColdFactory() -> None:
        pytest.fail("Async cloud input constructed a synchronous credential/transfer adapter")

    async def respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        calls.append(request.method)
        if request.method == "PUT":
            stored[request.url.path] = request.content
            return httpx.Response(200)
        return httpx.Response(200, content=stored[request.url.path])

    def createClient(**options: object) -> httpx.AsyncClient:
        return clientType(transport=httpx.MockTransport(respond), trust_env=False)

    async def rejectExecutor(*arguments: object, **options: object) -> None:
        pytest.fail("Manual input work was detached to an executor")

    monkeypatch.setattr(settings, "S3_TYPE", "s3")
    monkeypatch.setattr(storageModule, "get_cached_storage_adapter", rejectColdFactory)
    monkeypatch.setattr(AsyncStoragePresigner, "generateUrl", presign)
    monkeypatch.setattr(httpx, "AsyncClient", createClient)
    monkeypatch.setattr(asyncio, "to_thread", rejectExecutor)
    storage = storageModule.LexicalInputStorage()
    deadline: IndexingDeadline = IndexingDeadline(1)
    payload: LexicalInput = LexicalInput(compilerFingerprint="frozen", units=())
    key, checksum, byteCount = await storage.saveAsync(jobId="async-job", revisionId="async-revision", payload=payload, deadline=deadline)
    assert byteCount > 0
    assert await storage.loadAsync(key=key, checksum=checksum, deadline=deadline) == payload
    assert calls == ["PUT", "GET"]


@pytest.mark.parametrize("outcome", ["completed", "unavailable", "invalid", "oversized", "timeout", "cancelled"])
@pytest.mark.asyncio
async def test_manual_retry_waits_in_original_async_handler_until_publication(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    from fastapi import Response
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.services.jobs.indexing_retry_service import retryIndexing
    from shared.core.config import settings
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.asynccoordination import AsyncLexicalCoordination
    from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
    from shared.services.retrieval.lexical.bulk import LexicalBulk
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.preparation import LexicalPreparation
    from shared.services.retrieval.lexical.unit import LexicalUnit
    from shared.core.exceptions.knowhere_exception import KnowhereException

    LexicalPreparation.warmCompiler()  # Match the API lifespan before accepting a request.
    engine, jobId = parsedRuntime
    scope: LexicalScope = LexicalScope(ownerId="execution-user", namespace="execution-contract")
    with Session(engine) as session:
        job = session.get(Job, jobId)
        assert job is not None
        job.status = "failed"
        job.indexing_status = "failed"
        job.failure_stage = "indexing"
        attempt = session.scalars(select(IndexingAttempt)).one()
        attempt.state = "failed"
        session.add(LexicalNamespaceIndex(id=scope.fingerprint, ownerId=scope.ownerId,
            namespace=scope.namespace, corpusKind="private", indexName=f"{settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX}-{scope.fingerprint[:48]}",
            indexUuid="async-native-uuid", state="open"))
        session.commit()
        originalResult: str = session.scalars(select(JobResult.id)).one()

    entered: asyncio.Event = asyncio.Event()
    release: asyncio.Event = asyncio.Event()
    writes: list[str] = []
    copies: list[str] = []

    def submitCopy(buildId: str) -> bool:
        with Session(engine) as session:
            job = session.get(Job, jobId)
            assert job is not None and job.status == "done"
        copies.append(buildId)
        raise RuntimeError("Injected optional pool submission failure")

    async def ensure(self: AsyncLexicalCoordination, **options: object) -> tuple[str, int]:
        return "async-native-uuid", 1

    async def getIdentity(self: AsyncOpenSearchLexicalIndex) -> str:
        return "async-native-uuid"

    async def write(self: AsyncOpenSearchLexicalIndex, *, buildId: str, units: tuple[LexicalUnit, ...],
                    beforeBatch: object = None) -> None:
        writes.append(buildId)
        entered.set()
        await release.wait()
        if outcome == "unavailable":
            raise LexicalIndexError("Injected native failure", isRetryable=True)
        if outcome == "invalid":
            raise LexicalIndexError("Injected invalid retained input")
        if outcome == "oversized":
            tuple(LexicalBulk.createRequests(buildId=buildId, units=units, maximumBytes=1))
        if outcome == "timeout":
            raise TimeoutError("Injected whole-operation deadline")

    async def rejectExecutor(*arguments: object, **options: object) -> None:
        raise AssertionError("Primary indexing was transferred to an executor")

    monkeypatch.setattr(AsyncLexicalCoordination, "ensureIndex", ensure)
    monkeypatch.setattr(AsyncOpenSearchLexicalIndex, "getIndexUuid", getIdentity)
    monkeypatch.setattr(AsyncOpenSearchLexicalIndex, "writeBuild", write)
    monkeypatch.setattr(LegacyCopyDispatch, "submit", submitCopy)
    monkeypatch.setattr(asyncio, "to_thread", rejectExecutor)
    asyncEngine = create_async_engine(get_contract_database_url())
    response: Response = Response()
    try:
        async with AsyncSession(asyncEngine, expire_on_commit=False) as db:
            task = asyncio.create_task(retryIndexing(db, jobId=jobId, userId="execution-user", response=response))
            try:
                try:
                    # This is a synchronization watchdog, not the service SLA.
                    # A production timeout still settles and fails the task.
                    await asyncio.wait_for(entered.wait(), timeout=(
                        settings.KNOWHERE_INDEXING_TIMEOUT_SECONDS + settings.KNOWHERE_INDEXING_SETTLEMENT_TIMEOUT_SECONDS
                    ))
                except TimeoutError:
                    if task.done():
                        await task
                    raise
                assert not task.done()
                await asyncio.sleep(0)
                assert not task.done()
                async with AsyncSession(asyncEngine, expire_on_commit=False) as second:
                    with pytest.raises(KnowhereException) as conflict:
                        await retryIndexing(second, jobId=jobId, userId="execution-user", response=Response())
                    assert conflict.value.http_status_code == 409
                if outcome == "cancelled":
                    task.cancel()
                release.set()
                if outcome == "completed":
                    result = await task
                    assert result.status == "done" and response.status_code == 200
                    assert result.job_result_id == originalResult
                elif outcome == "cancelled":
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(KnowhereException) as failed:
                        await task
                    assert failed.value.http_status_code == {"unavailable": 503, "invalid": 500, "oversized": 500, "timeout": 504}[outcome]
                    assert failed.value.details["job_result_id"] == originalResult
                    assert failed.value.details["parsing_status"] == "completed"
                    assert failed.value.details["indexing_status"] == "failed"
                    if outcome == "oversized":
                        assert failed.value.details["reason"] == "INDEXING_INPUT_TOO_LARGE"
                        assert failed.value.details["job"]["error"]["code"] == "INDEXING_INPUT_TOO_LARGE"
                        assert "size limit" in failed.value.user_message
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
    finally:
        await asyncEngine.dispose()
    assert len(writes) == 1
    assert len(copies) == (1 if outcome == "completed" else 0)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1
        assert session.scalar(select(func.count()).select_from(IndexingAttempt)) == 2
        assert session.scalar(select(IndexingAttempt.executionCount).where(IndexingAttempt.attemptNumber == 2)) == 1
        job = session.get(Job, jobId)
        assert job is not None and job.billing_status == "charged" and job.credits_charged == 123456
        assert job.indexing_status == ("completed" if outcome == "completed" else "failed")


@pytest.mark.parametrize("isAsync", [False, True])
def test_postgresql_primary_uses_same_attempt_without_native_storage(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch, isAsync: bool,
) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from shared.core.config import settings
    from shared.models.database.document import Document
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.lexical.asyncdatabase import AsyncLexicalDatabase
    from shared.services.retrieval.lexical.asyncworker import AsyncIndexingWorker
    from shared.services.retrieval.lexical.deadline import IndexingDeadline
    from shared.services.retrieval.lexical.worker import IndexingWorker

    engine, jobId = parsedRuntime
    monkeypatch.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "postgresql")
    with Session(engine) as session:
        control = session.get(LexicalBackendControl, 1)
        assert control is not None
        control.backend = "postgresql"
        attemptId: str = session.scalars(select(IndexingAttempt.id)).one()
        session.commit()

    async def runAsync() -> str:
        asyncEngine = create_async_engine(get_contract_database_url())
        try:
            worker = AsyncIndexingWorker(AsyncLexicalDatabase(asyncEngine),
                IndexingDeadline(settings.KNOWHERE_PG_INDEXING_TIMEOUT_SECONDS))
            return await worker.run(jobId, attemptId=attemptId)
        finally:
            await asyncEngine.dispose()

    result: str = asyncio.run(runAsync()) if isAsync else IndexingWorker(
        sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId)
    assert result == "completed"
    with Session(engine) as session:
        attempt = session.get(IndexingAttempt, attemptId)
        job = session.get(Job, jobId)
        document = session.get(Document, "doc_execution")
        assert attempt is not None and attempt.executionCount == 1 and attempt.state == "succeeded"
        assert (attempt.executionDeadlineAt - attempt.startedAt).total_seconds() == settings.KNOWHERE_PG_INDEXING_TIMEOUT_SECONDS
        assert job is not None and job.status == "done" and job.parsing_status == "completed"
        assert document is not None and document.current_job_result_id == document.latest_parsed_job_result_id
        assert document.current_indexing_build_id is None


def test_postgresql_parsing_commits_before_primary_publication(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core.config import settings
    from shared.models.database.job import Job
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer

    engine, _ = parsedRuntime
    monkeypatch.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "postgresql")
    jobId: str = _createUnparsedJob(engine)
    with Session(engine) as session:
        control = session.get(LexicalBackendControl, 1)
        assert control is not None
        control.backend = "postgresql"
        session.commit()
        result = SyncJobSuccessFinalizer().finalize(session, job_id=jobId,
            result_s3_key="results/pg-parsed.zip", checksum="pg-parsed", zip_size=10,
            chunks=[], stored_count=0, delivery_mode="url", section_summaries=None)
        session.commit()
        job = session.get(Job, jobId)
        assert job is not None and job.parsing_status == "completed" and job.indexing_status == "pending"
        assert job.status == "running"
        assert result.post_commit_effects.indexing_job_ids == (jobId,)
        assert session.scalar(select(IndexingAttempt.executionCount).where(IndexingAttempt.jobId == jobId)) == 0


def test_abandoned_indexing_sweep_settles_without_execution_or_refund(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.services.retrieval.lexical.settlement import IndexingSettlement

    engine, jobId = parsedRuntime
    with Session(engine) as session:
        now = session.scalar(select(func.clock_timestamp()))
        attempt = session.scalars(select(IndexingAttempt)).one()
        attempt.createdAt = now - timedelta(minutes=10)
        session.commit()
    counts = IndexingSettlement.sweep(sessionmaker(engine, expire_on_commit=False))
    assert counts == {"expired": 1, "deferred": 0}
    assert IndexingSettlement.sweep(sessionmaker(engine, expire_on_commit=False)) == {"expired": 0, "deferred": 0}
    with Session(engine) as session:
        job = session.get(Job, jobId)
        attempt = session.scalars(select(IndexingAttempt)).one()
        assert job is not None and job.status == "failed" and job.parsing_status == "completed"
        assert job.billing_status == "charged" and job.credits_charged == 123456
        assert attempt.state == "failed" and attempt.executionCount == 0


@pytest.mark.asyncio
async def test_retry_signing_failure_retains_authorized_phase_and_result_context(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import Response
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.services.jobs import indexing_retry_service
    from shared.core.exceptions.domain_exceptions import UnavailableException
    from shared.core.exceptions.knowhere_exception import KnowhereException
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job_result import JobResult
    from shared.models.schemas.job import JobResultResponse

    engine, jobId = parsedRuntime
    read = indexing_retry_service.get_job_result_for_user

    async def requireSigning(db: AsyncSession, job_id: str, user_id: str,
                             *, shouldSignResultURL: bool = True) -> JobResultResponse:
        if shouldSignResultURL:
            raise UnavailableException("Injected result credential unavailability", retry_after=1)
        return await read(db, job_id=job_id, user_id=user_id, shouldSignResultURL=False)

    monkeypatch.setattr(indexing_retry_service, "get_job_result_for_user", requireSigning)
    asyncEngine = create_async_engine(get_contract_database_url())
    try:
        async with AsyncSession(asyncEngine, expire_on_commit=False) as db:
            with pytest.raises(KnowhereException) as failed:
                await indexing_retry_service.retryIndexing(db, jobId=jobId, userId="execution-user", response=Response())
            assert failed.value.http_status_code == 503
            with Session(engine) as session:
                assert failed.value.details["job_result_id"] == session.scalar(select(JobResult.id).where(JobResult.job_id == jobId))
                assert session.scalar(select(func.count()).select_from(IndexingAttempt)) == 1
                assert session.scalar(select(IndexingAttempt.executionCount)) == 0
            assert failed.value.details["parsing_status"] == "completed"
            assert failed.value.details["indexing_status"] == "pending"
    finally:
        await asyncEngine.dispose()


def test_expired_activation_does_not_commit_prepared_publication(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from shared.models.database.document import Document
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.models.database.webhook import WebhookEvent
    from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox, WebhookOutboxEvent
    from shared.services.retrieval.lexical import deadline
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.worker import IndexingWorker

    engine, jobId = parsedRuntime
    factory: sessionmaker[Session] = sessionmaker(engine, expire_on_commit=False)
    scope: LexicalScope = LexicalScope(ownerId="execution-user", namespace="execution-contract")
    with factory.begin() as db:
        db.add(LexicalNamespaceIndex(id=scope.fingerprint, ownerId=scope.ownerId,
            namespace=scope.namespace, corpusKind="private", indexName="test-activation",
            indexUuid="test-activation-uuid", state="open"))
    monkeypatch.setattr(LexicalCoordination, "ensureIndex", lambda self, **options: ("test-activation-uuid", 1))
    monkeypatch.setattr(OpenSearchLexicalIndex, "getIndexUuid", lambda self: "test-activation-uuid")
    writes: list[str] = []

    def writeBuild(self: OpenSearchLexicalIndex, *, buildId: str, **options: object) -> None:
        writes.append(buildId)

    monkeypatch.setattr(OpenSearchLexicalIndex, "writeBuild", writeBuild)
    monkeypatch.setattr(LegacyCopyDispatch, "submit", staticmethod(lambda _buildId: False))
    elapsed: list[float] = [0.0]
    monkeypatch.setattr(deadline, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    createEvent = SyncJobWebhookOutbox.create_event
    expiredBeforeCommit: list[str] = []

    def expireAfterEvent(self: SyncJobWebhookOutbox, db: Session, *, job_id: str,
                         event_type: str) -> WebhookOutboxEvent | None:
        event: WebhookOutboxEvent | None = createEvent(self, db, job_id=job_id, event_type=event_type)
        if event_type == "job.completed":
            expiredBeforeCommit.append(job_id)
            elapsed[0] += 20
        return event

    monkeypatch.setattr(SyncJobWebhookOutbox, "create_event", expireAfterEvent)
    worker: IndexingWorker = IndexingWorker(sessionFactory=factory)
    assert worker.run(jobId) == "failed"
    assert expiredBeforeCommit == [jobId] and len(writes) == 1
    with factory.begin() as db:
        job: Job = db.scalars(select(Job)).one()
        attempt: IndexingAttempt = db.scalars(select(IndexingAttempt)).one()
        document: Document = db.scalars(select(Document)).one()
        build: LexicalBuild = db.scalars(select(LexicalBuild)).one()
        assert job.error_code == "DEADLINE_EXCEEDED" and job.parsing_status == "completed"
        assert job.billing_status == "charged" and job.credits_charged == 123456
        assert attempt.state == "failed" and attempt.executionCount == 1
        assert document.current_job_result_id is None and document.current_indexing_build_id is None
        assert build.state == "failed"
        assert sorted(event.payload["event"] for event in db.scalars(select(WebhookEvent))) == ["job.failed", "job.parsed"]
