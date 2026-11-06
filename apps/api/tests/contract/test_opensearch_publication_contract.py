"""Prepared parses become searchable only after native visibility and activation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
import json
import os
from datetime import timedelta
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
from typing import ParamSpec, TypeVar

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    clear_application_modules,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)

_Parameters = ParamSpec("_Parameters")
_Result = TypeVar("_Result")


def _inspectAsyncCall(
    original: Callable[_Parameters, Awaitable[_Result]], inspect: Callable[[], None],
) -> Callable[_Parameters, Awaitable[_Result]]:
    async def inspected(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> _Result:
        inspect()
        return await original(*args, **kwargs)
    return inspected


def test_completed_parse_retains_result_without_publishing_or_writing_tokens(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    clear_application_modules()
    asyncio.run(prepare_contract_storage())

    from shared.models.database.document import Document, DocumentMapUnitToken
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.user import User
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer

    engine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))
    try:
        with Session(engine) as session:
            session.add(User(id="user-lexical-contract", name="Lexical contract user", email="lexical-contract@example.invalid"))
            session.flush()
            session.add(Job(
                job_id="job-lexical-contract",
                user_id="user-lexical-contract",
                job_type="document_ingestion",
                source_type="file",
                status="running",
                job_metadata={"document_id": "doc_lexical_contract", "namespace": "lexical-contract", "source_file_name": "report.pdf"},
            ))
            session.commit()

        with Session(engine) as session:
            finalization = SyncJobSuccessFinalizer().finalize(
                session,
                job_id="job-lexical-contract",
                result_s3_key="results/lexical-contract.zip",
                checksum="contract-checksum",
                zip_size=100,
                chunks=[{"chunk_id": "chunk-lexical-contract", "type": "text", "content": "工程验收计划", "path": "report.pdf/工程验收", "metadata": {}}],
                stored_count=1,
                delivery_mode="url",
                section_summaries=None,
            )
            assert finalization.response.should_commit()
            session.commit()

        with Session(engine) as session:
            document = session.get(Document, "doc_lexical_contract")
            job = session.get(Job, "job-lexical-contract")
            result = session.scalar(select(JobResult).where(JobResult.job_id == "job-lexical-contract"))
            assert document is not None
            assert job is not None
            assert result is not None
            assert document.current_job_result_id is None
            assert document.latest_parsed_job_result_id == result.id
            assert job.status == "running"
            assert job.parsing_status == "completed"
            assert job.indexing_status == "pending"
            assert result.result_s3_key == "results/lexical-contract.zip"
            assert session.scalar(select(func.count()).select_from(DocumentMapUnitToken)) == 0
            assert finalization.post_commit_effects.indexing_job_ids == (job.job_id,)
    finally:
        engine.dispose()


def _getNativeEndpoint() -> str:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if not endpoint:
        pytest.skip("Set KNOWHERE_TEST_OPENSEARCH_URL to an owned isolated native node")
    return endpoint


def _prepareParsedJob(
    monkeypatch: pytest.MonkeyPatch, postgresqlProcess: PostgreSQLProcess,
    *, webhookEvents: list[str] | None = None,
    content: str = "工程验收计划",
    preparedChunks: list[dict[str, object]] | None = None,
) -> tuple[Engine, str, str]:
    configure_contract_environment(monkeypatch, postgresqlProcess)
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_URL", os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL") or "http://127.0.0.1:1")
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_INDEX_PREFIX", "knowhere-slice-test")
    clear_application_modules()
    asyncio.run(prepare_contract_storage())
    from shared.models.database.job import Job
    from shared.models.database.user import User
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer

    engine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))
    namespace: str = f"publication-{uuid4().hex}"
    with Session(engine) as session:
        session.add(User(id="native-contract-user", name="Native contract", email="native-contract@example.invalid"))
        session.flush()
        session.add(Job(job_id="native-contract-job", user_id="native-contract-user",
            job_type="document_ingestion", source_type="file", status="running",
            webhook_url="https://example.invalid/callback" if webhookEvents else None,
            webhook_enabled=bool(webhookEvents), webhook_events=webhookEvents,
            credits_charged=123456, billing_status="charged",
            job_metadata={"document_id": "doc_native_contract", "namespace": namespace, "source_file_name": "report.pdf"}))
        session.commit()
        finalization = SyncJobSuccessFinalizer().finalize(session,
            job_id="native-contract-job", result_s3_key="results/native-contract.zip", checksum="native-checksum",
            zip_size=100, chunks=preparedChunks if preparedChunks is not None else [{"chunk_id": "native-contract-chunk", "type": "text", "content": content, "path": "report.pdf/工程验收", "metadata": {}}],
            stored_count=len(preparedChunks) if preparedChunks is not None else 1,
            delivery_mode="url", section_summaries=None)
        assert finalization.response.should_commit()
        session.commit()
    return engine, "native-contract-job", namespace


def test_background_indexing_activates_the_visible_native_build(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if endpoint is None:
        pytest.skip("Set KNOWHERE_TEST_OPENSEARCH_URL to an isolated test node")
    from sqlalchemy.orm import sessionmaker
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc)
    index = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace),
        indexPrefix="knowhere-slice-test")
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    monkeypatch.setattr(LegacyCopyDispatch, "submit", staticmethod(lambda identity: False))
    try:
        from shared.services.retrieval.lexical.worker import IndexingWorker
        worker = IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False))
        outcome: str = worker.run(jobId)
        from shared.models.database.indexing import IndexingAttempt
        with Session(engine) as session:
            diagnostic = session.scalar(select(IndexingAttempt.errorMessage).where(IndexingAttempt.jobId == jobId))
        assert outcome == "completed", diagnostic
        from shared.models.database.document import Document, DocumentChunk, DocumentMapUnitToken
        from shared.models.database.job import Job
        from shared.services.retrieval.scoring.knowhere_hybrid import tokenize_query_for_ranker
        with Session(engine) as session:
            document = session.get(Document, "doc_native_contract")
            job = session.get(Job, jobId)
            assert document is not None and job is not None
            assert job.status == "done"
            assert job.parsing_status == job.indexing_status == "completed"
            assert document.current_job_result_id == document.latest_parsed_job_result_id
            assert document.current_indexing_build_id is not None
            hits = index.search(buildIds=(document.current_indexing_build_id,),
                queryTokens=tuple(tokenize_query_for_ranker("工程验收计划")), limit=20)
            assert hits
            assert all(hit.unit.revisionId == document.current_job_result_id for hit in hits)
            assert session.scalar(select(func.count()).select_from(DocumentChunk)) == 1
            assert session.scalar(select(func.count()).select_from(DocumentMapUnitToken)) == 0
    finally:
        response = httpx.delete(f"{endpoint}/{index.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        index.close()
        engine.dispose()


def test_late_parser_failure_cannot_refund_or_fail_a_completed_parse(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    engine, jobId, _ = _prepareParsedJob(monkeypatch, postgresql_proc)
    from shared.models.database.job import Job
    from shared.services.jobs.lifecycle.failure_finalizer import SyncJobFailureFinalizer
    try:
        with Session(engine) as session:
            outcome = SyncJobFailureFinalizer().finalize(session, job_id=jobId,
                error_message="Delayed parser failure", error_code="INTERNAL_ERROR",
                error_details=None, should_refund=True)
            session.commit()
            assert not outcome.succeeded
            job = session.get(Job, jobId)
            assert job is not None
            assert job.status == "running" and job.parsing_status == "completed"
            assert job.indexing_status == "pending" and job.billing_status == "charged"
    finally:
        engine.dispose()


def test_parsed_and_completed_events_preserve_distinct_phase_snapshots(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str = _getNativeEndpoint()
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc,
        webhookEvents=["job.parsed", "job.completed"])
    from sqlalchemy.orm import sessionmaker
    from shared.models.database.webhook import WebhookEvent
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    index = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    try:
        with Session(engine) as session:
            event = session.scalar(select(WebhookEvent).where(WebhookEvent.job_id == jobId))
            assert event is not None
            parsedId: str = event.id
            assert event.payload["event"] == "job.parsed"
            assert event.payload["status"] == "running"
            assert event.payload["parsing_status"] == "completed"
            assert event.payload["indexing_status"] == "pending"
            assert event.milestone_key == f"{jobId}:job.parsed:job"
            from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox
            replay = SyncJobWebhookOutbox().create_event(session, job_id=jobId, event_type="job.parsed")
            assert replay is not None and replay.event_id == parsedId
            session.commit()
        worker = IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False))
        assert worker.run(jobId) == "completed"
        assert worker.run(jobId) == "completed"
        with Session(engine) as session:
            events = list(session.scalars(select(WebhookEvent).where(WebhookEvent.job_id == jobId)))
            assert len(events) == 2
            original = session.get(WebhookEvent, parsedId)
            assert original is not None and original.payload["indexing_status"] == "pending"
            completed = next(event for event in events if event.payload["event"] == "job.completed")
            assert completed.payload["status"] == "completed"
            assert completed.payload["indexing_status"] == "completed"
            assert completed.payload["event_id"] == completed.id
    finally:
        httpx.delete(f"{endpoint}/{index.indexName}", timeout=15, trust_env=False).raise_for_status()
        index.close()
        engine.dispose()


def test_legacy_frequency_copy_runs_only_after_native_publication(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str = _getNativeEndpoint()
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc)
    from sqlalchemy.orm import sessionmaker
    from shared.models.database.document import DocumentMapUnitToken
    from shared.models.database.job import Job
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    index = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    monkeypatch.setattr(LegacyCopyDispatch, "submit", staticmethod(lambda identity: False))
    try:
        factory = sessionmaker(engine, expire_on_commit=False)
        assert IndexingWorker(sessionFactory=factory).run(jobId) == "completed"
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(DocumentMapUnitToken)) == 0
            buildId: str = session.scalars(select(LexicalBuild.id)).one()
            job = session.get(Job, jobId)
            assert job is not None and job.status == "done"
        from shared.services.retrieval.lexical.legacy import LegacyLexicalCopy
        assert LegacyLexicalCopy(sessionFactory=factory).run(buildId)
        with Session(engine) as session:
            assert session.scalars(select(func.count()).select_from(DocumentMapUnitToken)).one() > 0
            build = session.get(LexicalBuild, buildId)
            assert build is not None and build.legacyCopyState == "completed"
            job = session.get(Job, jobId)
            assert job is not None and job.status == "done"
        assert asyncio.run(_discoverNative(namespace))[0]["content"] == "工程验收计划"
    finally:
        httpx.delete(f"{endpoint}/{index.indexName}", timeout=15, trust_env=False).raise_for_status()
        index.close()
        engine.dispose()


def test_public_job_reads_expose_parsed_result_before_search_readiness(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    engine, jobId, _ = _prepareParsedJob(monkeypatch, postgresql_proc)

    async def readJob() -> None:
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from app.services.jobs.read_service import get_job_result_for_user
        nativeEngine = create_async_engine(get_contract_database_url())
        try:
            async with AsyncSession(nativeEngine, expire_on_commit=False) as session:
                response = await get_job_result_for_user(session, job_id=jobId, user_id="native-contract-user")
                payload = response.model_dump()
                assert payload["status"] == "running"
                assert payload["parsing_status"] == "completed"
                assert payload["indexing_status"] == "pending"
                assert payload["result_url"]
        finally:
            await nativeEngine.dispose()

    try:
        asyncio.run(readJob())
    finally:
        engine.dispose()


def test_new_job_admission_fences_the_previous_failed_candidate(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc)
    from shared.models.database.job import Job
    from shared.services.retrieval.lexical.retry import IndexingRetry
    from shared.core.exceptions.domain_exceptions import ConflictException
    with Session(engine) as session:
        job = session.get(Job, jobId)
        assert job is not None
        job.status, job.indexing_status, job.failure_stage = "failed", "failed", "indexing"
        from shared.models.database.indexing import IndexingAttempt
        attempt = session.get(IndexingAttempt, job.current_indexing_attempt_id)
        assert attempt is not None
        attempt.state = "failed"
        session.commit()

    async def admit() -> None:
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from app.repositories.job_repository import JobRepository
        nativeEngine = create_async_engine(get_contract_database_url())
        try:
            async with AsyncSession(nativeEngine, expire_on_commit=False) as session:
                job = await JobRepository().create_job(session, user_id="native-contract-user",
                    job_id="newly-accepted-job", job_type="document_ingestion", source_type="file",
                    metadata={"document_id": "doc_native_contract", "namespace": namespace})
                assert job is not None
        finally:
            await nativeEngine.dispose()
    try:
        asyncio.run(admit())
        with Session(engine) as session:
            with pytest.raises(ConflictException) as captured:
                IndexingRetry().request(session, jobId=jobId, userId="native-contract-user")
            assert captured.value.details["reason"] == "INDEXING_SUPERSEDED"
    finally:
        engine.dispose()


def test_default_document_content_uses_latest_parse_while_retrieval_keeps_old_build(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str = _getNativeEndpoint()
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc)
    from sqlalchemy.orm import sessionmaker
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    index = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")

    async def readContent() -> None:
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from app.services.documents.lifecycle_service import DocumentService
        nativeEngine = create_async_engine(get_contract_database_url())
        try:
            async with AsyncSession(nativeEngine, expire_on_commit=False) as session:
                result = await DocumentService().list_document_chunks(session, user_id="native-contract-user",
                    document_id="doc_native_contract", page=1, page_size=20, chunk_type=None, include_asset_urls=False)
                assert result is not None
                assert result["chunks"][0]["content"] == "工程验收计划更新说明"
        finally:
            await nativeEngine.dispose()
    try:
        assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId) == "completed"
        _stageReplacement(engine, namespace)
        asyncio.run(readContent())
        assert asyncio.run(_discoverNative(namespace))[0]["content"] == "工程验收计划"
    finally:
        response: httpx.Response = httpx.delete(f"{endpoint}/{index.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        index.close()
        engine.dispose()


def test_archival_ends_pending_indexing_without_refund_or_late_activation(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    engine, jobId, _ = _prepareParsedJob(monkeypatch, postgresql_proc)
    from shared.models.database.job import Job

    async def archive() -> None:
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from app.services.documents.lifecycle_service import DocumentService
        nativeEngine = create_async_engine(get_contract_database_url())
        try:
            async with AsyncSession(nativeEngine, expire_on_commit=False) as session:
                result = await DocumentService().archive_document(session, user_id="native-contract-user",
                    document_id="doc_native_contract")
                assert result is not None and result["status"] == "archived"
        finally:
            await nativeEngine.dispose()
    try:
        asyncio.run(archive())
        with Session(engine) as session:
            job = session.get(Job, jobId)
            assert job is not None
            assert job.status == "failed" and job.failure_stage == "indexing"
            assert job.parsing_status == "completed" and job.billing_status == "charged"
        from sqlalchemy.orm import sessionmaker
        from shared.services.retrieval.lexical.worker import IndexingWorker
        assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId) == "failed"
    finally:
        engine.dispose()


def test_separate_gevent_process_completes_initial_indexing_inline(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str = _getNativeEndpoint()
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc)
    from shared.testing.contract_runtime import configure_runtime_database_role
    asyncio.run(configure_runtime_database_role(monkeypatch, get_contract_database_url()))
    from shared.models.database.job import Job
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    index = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    environment: dict[str, str] = dict(os.environ)
    root: Path = Path(__file__).resolve().parents[4]
    environment["PYTHONPATH"] = f"{root / 'packages/shared-python'}:{root}"
    try:
        completed = subprocess.run([sys.executable, "-c",
            "import gevent.monkey; gevent.monkey.patch_all(); "
            "from psycogreen.gevent import patch_psycopg; patch_psycopg(); "
            "from shared.services.jobs.lifecycle.post_commit_effects import PostCommitEffectPlan, SyncJobPostCommitEffectRunner; "
            f"SyncJobPostCommitEffectRunner().run(PostCommitEffectPlan(indexing_job_ids=({jobId!r},)))"],
            cwd=root, env=environment, capture_output=True, timeout=20)
        assert completed.returncode == 0, completed.stderr.decode()[-2000:]
        with Session(engine) as session:
            job = session.get(Job, jobId)
            assert job is not None and job.status == "done" and job.indexing_status == "completed"
        assert asyncio.run(_discoverNative(namespace))[0]["content"] == "工程验收计划"
    finally:
        response: httpx.Response = httpx.delete(f"{endpoint}/{index.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        index.close()
        engine.dispose()


def test_expired_writer_cannot_replace_the_build_published_after_manual_retry(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str = _getNativeEndpoint()
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc)
    from sqlalchemy.orm import sessionmaker
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.document import Document
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.lexicallease import LexicalLease
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.unit import LexicalUnit
    from shared.services.retrieval.lexical.cleanup import LexicalCleanup
    factory = sessionmaker(engine, expire_on_commit=False)
    worker = IndexingWorker(sessionFactory=factory)
    native = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    writeBuild = OpenSearchLexicalIndex.writeBuild
    expiredBuilds: list[str] = []

    def takeOver(self: OpenSearchLexicalIndex, *, buildId: str, units: tuple[LexicalUnit, ...],
                 beforeBatch: Callable[[], None] | None = None) -> None:
        writeBuild(self, buildId=buildId, units=units, beforeBatch=beforeBatch)
        if expiredBuilds:
            return
        expiredBuilds.append(buildId)
        with Session(engine) as session:
            now = LexicalCoordination.getDatabaseTime(session)
            attempt = session.scalars(select(IndexingAttempt).where(IndexingAttempt.jobId == jobId)).one()
            attempt.claimExpiresAt = now - timedelta(seconds=1)
            attempt.executionDeadlineAt = now - timedelta(seconds=1)
            lease = session.scalars(select(LexicalLease).where(LexicalLease.buildId == buildId)).one()
            lease.expiresAt = now - timedelta(seconds=1)
            session.commit()
        assert IndexingWorker(sessionFactory=factory).run(jobId) == "failed"
        from shared.services.retrieval.lexical.retry import IndexingRetry
        with Session(engine) as session:
            assert IndexingRetry().request(session, jobId=jobId, userId="native-contract-user")
            session.commit()
        assert IndexingWorker(sessionFactory=factory).run(jobId) == "completed"
        assert not LexicalCleanup(sessionFactory=factory).collect(buildId)

    try:
        monkeypatch.setattr(OpenSearchLexicalIndex, "writeBuild", takeOver)
        assert worker.run(jobId) == "completed"
        with Session(engine) as session:
            document = session.get(Document, "doc_native_contract")
            assert document is not None and document.current_indexing_build_id != expiredBuilds[0]
            obsolete = session.get(LexicalBuild, expiredBuilds[0])
            assert obsolete is not None and obsolete.state == "failed"
            attempts = list(session.scalars(select(IndexingAttempt).where(IndexingAttempt.jobId == jobId).order_by(IndexingAttempt.attemptNumber)))
            assert [attempt.executionCount for attempt in attempts] == [1, 1]
            assert [attempt.state for attempt in attempts] == ["failed", "succeeded"]
        assert LexicalCleanup(sessionFactory=factory).collect(expiredBuilds[0])
        assert asyncio.run(_discoverNative(namespace))[0]["content"] == "工程验收计划"
    finally:
        response: httpx.Response = httpx.delete(f"{endpoint}/{native.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        native.close()
        engine.dispose()


def test_blocked_cleanup_does_not_starve_later_rollback_copy_work(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str = _getNativeEndpoint()
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc)
    from sqlalchemy.orm import sessionmaker
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.cleanup import LexicalCleanup
    native = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    from shared.services.retrieval.lexical.legacy import LegacyLexicalCopy
    monkeypatch.setattr(LegacyCopyDispatch, "submit", staticmethod(lambda identity: False))
    try:
        assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId) == "completed"
        with Session(engine) as session:
            published = session.scalars(select(LexicalBuild)).one()
            publishedId: str = published.id
            for position in range(20):
                session.add(LexicalBuild(attemptId=published.attemptId,
                    namespaceIndexId=published.namespaceIndexId, documentId=published.documentId,
                    revisionId=published.revisionId, ownerToken=str(uuid4()), state="failed", cleanupState="pending",
                    createdAt=published.createdAt - timedelta(seconds=position + 1)))
            session.commit()
        monkeypatch.setattr(LexicalCleanup, "collect", lambda self, buildId: False)
        def copyInline(identity: str) -> bool:
            return LegacyLexicalCopy(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(identity)
        monkeypatch.setattr(LegacyCopyDispatch, "submit", staticmethod(copyInline))
        from shared.services.retrieval.lexical.maintenance import maintainLexical
        maintainLexical.run()
        maintainLexical.run()
        with Session(engine) as session:
            published = session.get(LexicalBuild, publishedId)
            assert published is not None and published.legacyCopyState == "completed"
    finally:
        response: httpx.Response = httpx.delete(f"{endpoint}/{native.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        native.close()
        engine.dispose()


def test_parsed_revision_preserves_existing_nul_sanitization(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    engine, _, _ = _prepareParsedJob(monkeypatch, postgresql_proc, content="工程\x00验收")
    from shared.models.database.document import DocumentChunk
    try:
        with Session(engine) as session:
            assert session.scalars(select(DocumentChunk.content)).one() == "工程验收"
    finally:
        engine.dispose()


def test_native_activation_preserves_parser_track_and_document_metadata(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str = _getNativeEndpoint()
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc)
    from sqlalchemy.orm import sessionmaker
    from shared.models.database.job import Job
    from shared.models.database.document import Document
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    native = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    try:
        with Session(engine) as session:
            job = session.get(Job, jobId)
            assert job is not None
            job.job_metadata = {**dict(job.job_metadata or {}), "parse_track": "page_memory",
                                "document_metadata": {"client_version": "contract-test"}}
            session.commit()
        assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId) == "completed"
        with Session(engine) as session:
            document = session.get(Document, "doc_native_contract")
            assert document is not None
            assert document.parse_track == "page_memory"
            assert document.document_metadata == {"client_version": "contract-test"}
    finally:
        response: httpx.Response = httpx.delete(f"{endpoint}/{native.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        native.close()
        engine.dispose()


def test_obsolete_queue_reconciliation_never_reexecutes_and_sweep_settles_unstarted_intent(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    engine, jobId, _ = _prepareParsedJob(monkeypatch, postgresql_proc)
    from sqlalchemy.orm import sessionmaker
    from shared.core.celery_app import celery_app
    from shared.models.database.indexing import IndexingAttempt
    from shared.services.retrieval.lexical.dispatch import dispatchIndexing
    from shared.services.retrieval.lexical.reconcile import reconcileIndexing
    from shared.services.retrieval.lexical.coordination import LexicalCoordination

    def failDelivery(*args: object, **kwargs: object) -> None:
        raise ConnectionError("Injected broker outage")

    try:
        with monkeypatch.context() as fault:
            fault.setattr(celery_app, "send_task", failDelivery)
            assert not dispatchIndexing(jobId, sessionFactory=sessionmaker(engine, expire_on_commit=False))
        with Session(engine) as session:
            attempt = session.scalars(select(IndexingAttempt).where(IndexingAttempt.jobId == jobId)).one()
            assert attempt.state == "pending" and attempt.executionCount == 0
            attempt.nextDispatchAt = LexicalCoordination.getDatabaseTime(session) - timedelta(seconds=1)
            session.commit()
        monkeypatch.setattr(celery_app, "send_task", lambda *args, **kwargs: None)
        assert reconcileIndexing.run() == 0
        assert reconcileIndexing.run() == 0
        with Session(engine) as session:
            attempt = session.scalars(select(IndexingAttempt).where(IndexingAttempt.jobId == jobId)).one()
            attempt.admissionDeadlineAt = LexicalCoordination.getDatabaseTime(session) - timedelta(seconds=1)
            session.commit()
        from shared.services.retrieval.lexical.settlement import IndexingSettlement
        assert IndexingSettlement.sweep(sessionmaker(engine, expire_on_commit=False))["expired"] == 1
        with Session(engine) as session:
            attempt = session.scalars(select(IndexingAttempt).where(IndexingAttempt.jobId == jobId)).one()
            assert attempt.executionCount == 0 and attempt.state == "failed"
    finally:
        engine.dispose()


def test_parsed_notification_exposes_retained_result_without_changing_its_phase_snapshot(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    engine, jobId, _ = _prepareParsedJob(monkeypatch, postgresql_proc, webhookEvents=["job.parsed"])
    from shared.models.database.webhook import WebhookEvent
    from shared.services.webhook.qstash_payload import QStashPayloadEnricher
    try:
        with Session(engine) as session:
            event = session.scalars(select(WebhookEvent).where(WebhookEvent.job_id == jobId)).one()
            payload = QStashPayloadEnricher().enrich(session, event)
            assert payload.get("result_url")
            assert payload.get("job_result_id")
            assert payload["status"] == "running" and payload["indexing_status"] == "pending"
    finally:
        engine.dispose()


def test_demo_native_publication_and_reads_respect_the_runtime_database_role(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str = _getNativeEndpoint()
    engine, _, _ = _prepareParsedJob(monkeypatch, postgresql_proc)
    from shared.core.config import settings
    from shared.models.database.demo_corpus import DemoDocument
    from shared.models.database.job import Job
    from shared.testing.contract_runtime import configure_runtime_database_role
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
    from shared.services.retrieval.corpus_storage import DEMO_NAMESPACE, DEMO_OWNER
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    from shared.services.retrieval.lexical.reader import PublishedLexicalRead
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.lexical.legacy import LegacyLexicalCopy
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.scoring.knowhere_hybrid import tokenize_query_for_ranker
    from sqlalchemy.orm import sessionmaker
    # This contract runs the copy under the same restricted role explicitly.
    monkeypatch.setattr(LegacyCopyDispatch, "submit", staticmethod(lambda _buildId: False))
    monkeypatch.setattr(settings, "DEMO_MAINTAINER_USER_IDS", "native-contract-user")
    documentId: str = f"ddoc_{uuid4().hex[:12]}"
    with Session(engine) as session:
        session.add(DemoDocument(document_id=documentId, demo_source_id="native-demo-source",
            title="Native demo", user_id=DEMO_OWNER, namespace=DEMO_NAMESPACE))
        session.add(Job(job_id="native-demo-job", user_id="native-contract-user", job_type="document_ingestion",
            source_type="file", status="running", job_metadata={"document_id": documentId,
                "namespace": DEMO_NAMESPACE, "corpus_target": "DEMO", "demo_source_id": "native-demo-source",
                "source_file_name": "demo.pdf"}))
        session.commit()
    asyncio.run(configure_runtime_database_role(monkeypatch, get_contract_database_url()))
    runtimeEngine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))
    factory = sessionmaker(runtimeEngine, expire_on_commit=False)
    native = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId=DEMO_OWNER, namespace=DEMO_NAMESPACE, corpusKind="demo"), indexPrefix="knowhere-slice-test")
    reader = PublishedLexicalRead(userId="ordinary-viewer", namespace=DEMO_NAMESPACE, sessionFactory=factory)
    try:
        with factory.begin() as session:
            SyncJobSuccessFinalizer().finalize(session, job_id="native-demo-job",
                result_s3_key="results/demo.zip", checksum="checksum", zip_size=100,
                chunks=[{"chunk_id": "demo-chunk", "type": "text", "content": "工程验收计划", "path": "demo.pdf/工程", "metadata": {}}],
                stored_count=1, delivery_mode="url", section_summaries=None)
        assert IndexingWorker(sessionFactory=factory).run("native-demo-job") == "completed"
        reader.start()
        buildId: str = reader.builds[documentId]
        assert reader.search(buildIds=(buildId,), queryTokens=tuple(tokenize_query_for_ranker("工程验收计划")), limit=20)
        assert LegacyLexicalCopy(sessionFactory=factory).run(buildId)
        from shared.services.retrieval.demo_authorization import authorize_demo_transaction
        from shared.services.retrieval.serving_generation import lock_namespace_generation, advance_namespace_generation
        from shared.services.retrieval.lexical.archival import IndexingArchival
        from shared.services.retrieval.lexical.cleanup import LexicalCleanup
        from sqlalchemy import text
        with factory.begin() as session:
            assert not session.scalar(text("SELECT current_setting('knowhere.demo_maintainer', true)"))
            authorize_demo_transaction(session, user_id="native-contract-user")
            lock_namespace_generation(session, user_id=DEMO_OWNER, namespace=DEMO_NAMESPACE)
            document = session.get(DemoDocument, documentId)
            assert document is not None
            document.status = "archived"
            generation: int = advance_namespace_generation(session, user_id=DEMO_OWNER, namespace=DEMO_NAMESPACE)
            IndexingArchival().finalize(session, userId="native-contract-user", documentId=documentId,
                namespace=DEMO_NAMESPACE, generation=generation)
        assert LexicalCleanup(sessionFactory=factory).collect(buildId)
        reader.close()
        assert LexicalCleanup(sessionFactory=factory).collect(buildId)
    finally:
        reader.close()
        response: httpx.Response = httpx.delete(f"{endpoint}/{native.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        native.close()
        runtimeEngine.dispose()
        engine.dispose()


def test_empty_demo_read_needs_no_write_authorization(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    engine, _, _ = _prepareParsedJob(monkeypatch, postgresql_proc)
    from shared.testing.contract_runtime import configure_runtime_database_role
    from shared.services.retrieval.corpus_storage import DEMO_NAMESPACE
    from shared.services.retrieval.lexical.reader import PublishedLexicalRead
    from sqlalchemy.orm import sessionmaker
    asyncio.run(configure_runtime_database_role(monkeypatch, get_contract_database_url()))
    runtimeEngine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))
    reader = PublishedLexicalRead(userId="ordinary-viewer", namespace=DEMO_NAMESPACE,
        sessionFactory=sessionmaker(runtimeEngine, expire_on_commit=False))
    try:
        reader.start()
        assert len(reader.pins) == 0 and reader.pins.generation == 0
    finally:
        reader.close()
        runtimeEngine.dispose()
        engine.dispose()


async def _discoverNative(namespace: str) -> list[dict[str, object]]:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from shared.services.retrieval.search.map_unit_discovery import map_unit_discovery
    engine = create_async_engine(get_contract_database_url())
    try:
        async with AsyncSession(engine) as session:
            result = await map_unit_discovery(session,
                user_id="native-contract-user", namespace=namespace,
                query="工程验收计划", top_k=20, exclude_document_ids=[], exclude_sections=[],
                publish_index_readiness=False)
            return result.payload["fused_rows"]
    finally:
        from shared.services.retrieval.lexical.asyncruntime import AsyncOpenSearchRuntime
        await AsyncOpenSearchRuntime.closeCurrent()
        await engine.dispose()


def test_existing_discovery_reads_native_build_without_pg_frequency_rows(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if endpoint is None:
        pytest.skip("Set KNOWHERE_TEST_OPENSEARCH_URL to an isolated test node")
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc)
    from sqlalchemy.orm import sessionmaker
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    index = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    try:
        assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId) == "completed"
        rows = asyncio.run(_discoverNative(namespace))
        assert rows
        assert rows[0]["document_id"] == "doc_native_contract"
        assert rows[0]["content"] == "工程验收计划"
    finally:
        response = httpx.delete(f"{endpoint}/{index.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        index.close()
        engine.dispose()


def test_native_failure_is_not_an_empty_or_pg_fallback_result(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if endpoint is None:
        pytest.skip("Set KNOWHERE_TEST_OPENSEARCH_URL to an isolated test node")
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc)
    from sqlalchemy.orm import sessionmaker
    from shared.core.config import settings
    from shared.core.exceptions.domain_exceptions import UnavailableException
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    index = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    try:
        assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId) == "completed"
        monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_URL", "http://127.0.0.1:1")
        with pytest.raises(UnavailableException):
            asyncio.run(_discoverNative(namespace))
    finally:
        response = httpx.delete(f"{endpoint}/{index.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        index.close()
        engine.dispose()


async def _retrievePublicResult(namespace: str) -> dict[str, object]:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from shared.services.retrieval.app_service import run_retrieval_query
    from shared.services.retrieval.stats.recorder import drain_retrieval_hit_stats_updates
    engine = create_async_engine(get_contract_database_url())
    try:
        async with AsyncSession(engine) as session:
            return await run_retrieval_query(db=session, user_id="native-contract-user",
                namespace=namespace, query="工程验收计划", top_k=1,
                exclude_document_ids=[], exclude_sections=[], use_agentic=False)
    finally:
        await drain_retrieval_hit_stats_updates()
        await engine.dispose()


def test_public_retrieval_cache_observes_activation_without_redis_invalidation(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if endpoint is None:
        pytest.skip("Set KNOWHERE_TEST_OPENSEARCH_URL to an isolated test node")
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc)
    from sqlalchemy.orm import sessionmaker
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    index = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    try:
        worker = IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False))
        assert worker.run(jobId) == "completed"
        first = asyncio.run(_retrievePublicResult(namespace))
        assert "工程验收计划" in str(first)
        with Session(engine) as session:
            document = session.get(Document, "doc_native_contract")
            assert document is not None
            session.add(Job(job_id="native-replacement-job", user_id="native-contract-user",
                job_type="document_ingestion", source_type="file", status="running",
                job_metadata={"document_id": document.document_id, "namespace": namespace, "source_file_name": "replacement.pdf"}))
            document.accepted_job_id = "native-replacement-job"
            session.commit()
            SyncJobSuccessFinalizer().finalize(session, job_id="native-replacement-job",
                result_s3_key="results/replacement.zip", checksum="replacement-checksum", zip_size=100,
                chunks=[{"chunk_id": "replacement-chunk", "type": "text", "content": "工程验收计划更新说明", "path": "replacement.pdf/工程验收", "metadata": {}}],
                stored_count=1, delivery_mode="url", section_summaries=None)
            session.commit()
        beforeActivation = asyncio.run(_retrievePublicResult(namespace))
        assert "工程验收计划更新说明" not in str(beforeActivation)
        assert worker.run("native-replacement-job") == "completed"
        afterActivation = asyncio.run(_retrievePublicResult(namespace))
        assert "工程验收计划更新说明" in str(afterActivation)
    finally:
        response = httpx.delete(f"{endpoint}/{index.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        index.close()
        engine.dispose()


def _stageReplacement(engine: Engine, namespace: str) -> str:
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
    with Session(engine) as session:
        document = session.get(Document, "doc_native_contract")
        assert document is not None
        jobId: str = "native-replacement-job"
        session.add(Job(job_id=jobId, user_id="native-contract-user", job_type="document_ingestion",
            source_type="file", status="running", billing_status="charged", credits_charged=345678,
            job_metadata={"document_id": document.document_id, "namespace": namespace, "source_file_name": "replacement.pdf"}))
        document.accepted_job_id = jobId
        session.commit()
        SyncJobSuccessFinalizer().finalize(session, job_id=jobId,
            result_s3_key="results/replacement.zip", checksum="replacement-checksum", zip_size=100,
            chunks=[{"chunk_id": "replacement-chunk", "type": "text", "content": "工程验收计划更新说明", "path": "replacement.pdf/工程验收", "metadata": {}}],
            stored_count=1, delivery_mode="url", section_summaries=None)
        session.commit()
        assert session.scalar(select(JobResult.id).where(JobResult.job_id == jobId))
        return jobId


def test_lost_native_ack_fails_one_execution_and_allows_free_indexing_retry(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if endpoint is None:
        pytest.skip("Set KNOWHERE_TEST_OPENSEARCH_URL to an isolated test node")
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc)
    from sqlalchemy.orm import sessionmaker
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.lexicallease import LexicalLease
    from shared.models.database.document import Document
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.unit import LexicalUnit
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    index = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    try:
        worker = IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False))
        assert worker.run(jobId) == "completed"
        replacement: str = _stageReplacement(engine, namespace)
        writeBuild = OpenSearchLexicalIndex.writeBuild

        def loseAcknowledgment(self: OpenSearchLexicalIndex, *, buildId: str,
                              units: tuple[LexicalUnit, ...],
                              beforeBatch: Callable[[], None] | None = None) -> None:
            writeBuild(self, buildId=buildId, units=units, beforeBatch=beforeBatch)
            raise LexicalIndexError("Injected lost native acknowledgment", isRetryable=True, hasUnknownOutcome=True)

        with monkeypatch.context() as failure:
            failure.setattr(OpenSearchLexicalIndex, "writeBuild", loseAcknowledgment)
            assert worker.run(replacement) == "failed"
            assert worker.run(replacement) == "failed"
        rows = asyncio.run(_discoverNative(namespace))
        assert rows[0]["content"] == "工程验收计划"
        with Session(engine) as session:
            job = session.get(Job, replacement)
            assert job is not None
            assert job.status == "failed"
            assert job.failure_stage == "indexing"
            assert job.parsing_status == "completed"
            assert job.billing_status == "charged" and job.credits_charged == 345678
            assert session.scalar(select(IndexingAttempt.executionCount).where(IndexingAttempt.jobId == replacement)) == 1
            assert session.scalar(select(func.count()).select_from(LexicalLease).where(LexicalLease.state == "uncertain")) == 1
        from shared.services.retrieval.lexical.retry import IndexingRetry
        with Session(engine) as session:
            assert IndexingRetry().request(session, jobId=replacement, userId="native-contract-user")
            session.commit()
            from shared.core.exceptions.domain_exceptions import ConflictException
            with pytest.raises(ConflictException):
                IndexingRetry().request(session, jobId=replacement, userId="native-contract-user")
            session.rollback()
        assert worker.run(replacement) == "completed"
        with Session(engine) as session:
            job = session.get(Job, replacement)
            document = session.get(Document, "doc_native_contract")
            assert job is not None and document is not None
            assert job.status == "done" and job.failure_stage is None
            assert job.billing_status == "charged" and job.credits_charged == 345678
            assert session.scalar(select(func.count()).select_from(IndexingAttempt).where(IndexingAttempt.jobId == replacement)) == 2
            assert document.current_job_result_id == document.latest_parsed_job_result_id
    finally:
        response = httpx.delete(f"{endpoint}/{index.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        index.close()
        engine.dispose()


def test_active_old_read_allows_cleanup_and_returns_actual_hits(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if endpoint is None:
        pytest.skip("Set KNOWHERE_TEST_OPENSEARCH_URL to an isolated test node")
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc)
    from sqlalchemy.orm import sessionmaker
    from shared.models.database.document import DocumentChunk
    from shared.services.retrieval.lexical.reader import PublishedLexicalRead
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    factory = sessionmaker(engine, expire_on_commit=False)
    index = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    reader = PublishedLexicalRead(userId="native-contract-user", namespace=namespace, sessionFactory=factory)
    try:
        worker = IndexingWorker(sessionFactory=factory)
        assert worker.run(jobId) == "completed"
        reader.start()
        oldBuild: str = reader.builds["doc_native_contract"]
        oldRevision: str = reader.pins["doc_native_contract"]
        replacement: str = _stageReplacement(engine, namespace)
        assert worker.run(replacement) == "completed"
        from shared.services.retrieval.lexical.cleanup import LexicalCleanup
        cleanup = LexicalCleanup(sessionFactory=factory)
        assert cleanup.collect(oldBuild)
        with reader.bind():
            assert asyncio.run(_discoverNative(namespace)) == []
        reader.close()
        assert cleanup.collect(oldBuild)
        assert not index.search(buildIds=(oldBuild,), queryTokens=("计划",), limit=20)
        with Session(engine) as session:
            assert session.scalar(select(DocumentChunk.content).where(DocumentChunk.job_result_id == oldRevision)) == "工程验收计划"
        assert asyncio.run(_discoverNative(namespace))[0]["content"] == "工程验收计划更新说明"
    finally:
        reader.close()
        response = httpx.delete(f"{endpoint}/{index.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        index.close()
        engine.dispose()


@pytest.fixture
def classicRuntime(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> Iterator[tuple[Engine, str]]:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if endpoint is None:
        pytest.skip("An owned OpenSearch node is required")
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc, preparedChunks=[
        {"chunk_id": "classic-first", "type": "text", "content": "工程验收计划", "path": "report.pdf/工程验收", "metadata": {}},
        {"chunk_id": "classic-second", "type": "text", "content": "项目质量管理程序", "path": "report.pdf/质量管理", "metadata": {}},
    ])
    from sqlalchemy.orm import sessionmaker
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.execution import plan

    async def skipCache(**options: object) -> tuple[None, None]:
        return None, None

    def skipStatistics(**options: object) -> None:
        pass

    monkeypatch.setattr(plan, "_read_cached_response", skipCache)
    monkeypatch.setattr(plan, "_schedule_hit_stats_update", skipStatistics)
    index = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    try:
        assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId) == "completed"
        yield engine, namespace
    finally:
        response: httpx.Response = httpx.delete(f"{endpoint}/{index.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        index.close()
        engine.dispose()


async def _queryClassic(namespace: str) -> dict[str, object]:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from shared.services.retrieval.execution.plan import run_retrieval_query

    database = create_async_engine(get_contract_database_url())
    try:
        async with AsyncSession(database) as session:
            return await run_retrieval_query(db=session, user_id="native-contract-user", namespace=namespace,
                query="工程验收计划", top_k=1, use_agentic=False, exclude_document_ids=[], exclude_sections=[])
    finally:
        from shared.services.retrieval.lexical.asyncruntime import AsyncOpenSearchRuntime
        await AsyncOpenSearchRuntime.closeCurrent()
        await database.dispose()


def _captureNativeSearchHits(response: httpx.Response, *, hasTokens: bool) -> str:
    body: object = response.json()
    assert isinstance(body, dict)
    envelope: object = body["hits"]
    assert isinstance(envelope, dict)
    hits: object = envelope["hits"]
    assert isinstance(hits, list)
    for hit in hits:
        assert isinstance(hit, dict)
        source: object = hit["_source"]
        assert isinstance(source, dict)
        for field in ("pathTokens", "contentTokens"):
            if hasTokens:
                tokens: object = source.pop(field)
                assert isinstance(tokens, str)
            else:
                assert field not in source
    return json.dumps(hits, sort_keys=True)


@pytest.mark.parametrize("isAsynchronous", [False, True])
def test_native_search_omits_tokens_without_changing_scores_or_revision_identity(
    classicRuntime: tuple[Engine, str], isAsynchronous: bool,
) -> None:
    from shared.models.database.document import Document
    from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
    from shared.services.retrieval.lexical.asyncruntime import AsyncOpenSearchRuntime
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.searchplan import NativeSearchPlan
    from shared.services.retrieval.scoring.knowhere_hybrid import tokenize_query_for_ranker

    engine, namespace = classicRuntime
    endpoint: str = _getNativeEndpoint()
    scope: LexicalScope = LexicalScope(ownerId="native-contract-user", namespace=namespace)
    index: OpenSearchLexicalIndex = OpenSearchLexicalIndex(
        endpoint=endpoint, scope=scope, indexPrefix="knowhere-slice-test")
    with Session(engine) as database:
        document: Document = database.scalars(select(Document)).one()
        buildId: str | None = document.current_indexing_build_id
    assert buildId is not None
    expected: list[str] = []
    observed: list[str] = []
    tokens: tuple[str, ...] = tuple(tokenize_query_for_ranker("工程验收计划"))
    with httpx.Client(base_url=endpoint, trust_env=False, timeout=30) as client:
        for channel in ("pathTokens", "contentTokens"):
            payload: dict[str, object] = NativeSearchPlan.buildPayload(
                channel, buildIds=(buildId,), queryTokens=tokens, limit=20, unitIds=None)
            payload.pop("_source", None)
            response: httpx.Response = client.post(f"/{index.indexName}/_search", json=payload)
            response.raise_for_status()
            expected.append(_captureNativeSearchHits(response, hasTokens=True))

    def captureResponse(response: httpx.Response) -> None:
        if response.request.url.path.endswith("/_search"):
            response.read()
            observed.append(_captureNativeSearchHits(response, hasTokens=False))

    async def searchAsync() -> None:
        async def captureAsyncResponse(response: httpx.Response) -> None:
            await response.aread()
            captureResponse(response)

        native: AsyncOpenSearchLexicalIndex = AsyncOpenSearchLexicalIndex(
            endpoint=endpoint, scope=scope, indexPrefix="knowhere-slice-test")
        runtime: AsyncOpenSearchRuntime = AsyncOpenSearchRuntime.resolve(endpoint)
        runtime.client.event_hooks["response"].append(captureAsyncResponse)
        try:
            assert await native.search(buildIds=(buildId,), queryTokens=tokens, limit=20)
        finally:
            runtime.client.event_hooks["response"].remove(captureAsyncResponse)
            native.close()
            await AsyncOpenSearchRuntime.closeCurrent()

    try:
        if isAsynchronous:
            asyncio.run(searchAsync())
        else:
            index._runtime.client.event_hooks["response"].append(captureResponse)
            try:
                assert index.search(buildIds=(buildId,), queryTokens=tokens, limit=20)
            finally:
                index._runtime.client.event_hooks["response"].remove(captureResponse)
        assert sorted(observed) == sorted(expected)
    finally:
        index.close()


def test_native_handles_reuse_the_http_connection_after_one_handle_closes(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core.config import settings
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    _, namespace = classicRuntime
    streams: list[object] = []

    def inspectResponses(send: Callable[_Parameters, httpx.Response]) -> Callable[_Parameters, httpx.Response]:
        def inspected(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> httpx.Response:
            response: httpx.Response = send(*args, **kwargs)
            streams.append(response.extensions.get("network_stream"))
            return response
        return inspected

    first = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace))
    second = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(httpx.Client, "send", inspectResponses(httpx.Client.send))
            first._request("GET", "/")
            first.close()
            second._request("GET", "/")
        assert len(streams) == 2 and streams[0] is not None
        assert streams[0] is streams[1], "Sequential native handles opened separate TCP connections"
    finally:
        first.close()
        second.close()


def test_classic_native_rpc_holds_no_sync_database_connection(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.pool import QueuePool
    from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
    from shared.services.retrieval.lexical.asyncread import AsyncPublishedLexicalRead

    _, namespace = classicRuntime
    original = AsyncOpenSearchLexicalIndex._request
    observed: list[bool] = []

    async def inspectRpc(self: AsyncOpenSearchLexicalIndex, method: str, path: str, *,
                         payload: object | None = None, textBody: str | None = None,
                         operationId: str | None = None, timeoutSeconds: float | None = None) -> object:
        if "/_search?" in path:
            reader = AsyncPublishedLexicalRead.current()
            assert isinstance(reader, AsyncPublishedLexicalRead)
            pool = reader.database.engine.pool
            assert isinstance(pool, QueuePool) and pool.checkedout() == 0
            observed.append(True)
        return await original(self, method, path, payload=payload, textBody=textBody,
            operationId=operationId, timeoutSeconds=timeoutSeconds)

    monkeypatch.setattr(AsyncOpenSearchLexicalIndex, "_request", inspectRpc)
    assert asyncio.run(_queryClassic(namespace))["results"]
    assert len(observed) == 2


def test_classic_read_keeps_captured_bindings_through_a_backend_epoch_change(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.models.database.lexicallease import LexicalLease
    from shared.services.retrieval.execution import routes

    engine, namespace = classicRuntime

    def switchBackend() -> None:
        with Session(engine) as database, database.begin():
            control = database.scalars(select(LexicalBackendControl).with_for_update()).one()
            control.epoch += 1

    monkeypatch.setattr(routes, "assemble_retrieval_results", _inspectAsyncCall(routes.assemble_retrieval_results, switchBackend))
    assert asyncio.run(_queryClassic(namespace))["results"]
    with Session(engine) as database:
        assert not list(database.scalars(select(LexicalLease).where(LexicalLease.state == "active")))


def test_classic_read_hydrates_its_hit_revision_after_replacement_and_cleanup(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.orm import sessionmaker
    from shared.models.database.document import Document
    from shared.services.retrieval.execution import routes
    from shared.services.retrieval.lexical.cleanup import LexicalCleanup
    from shared.services.retrieval.lexical.worker import IndexingWorker

    engine, namespace = classicRuntime
    with Session(engine) as database:
        document = database.scalars(select(Document)).one()
        oldBuild: str | None = document.current_indexing_build_id
        oldRevision: str | None = document.current_job_result_id
    assert oldBuild is not None and oldRevision is not None
    cleanup = LexicalCleanup(sessionFactory=sessionmaker(engine, expire_on_commit=False))

    def replaceDuringAssembly() -> None:
        jobId: str = _stageReplacement(engine, namespace)
        assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId) == "completed"
        assert cleanup.collect(oldBuild)

    monkeypatch.setattr(routes, "assemble_retrieval_results", _inspectAsyncCall(routes.assemble_retrieval_results, replaceDuringAssembly))
    response: dict[str, object] = asyncio.run(_queryClassic(namespace))
    results: object = response["results"]
    assert isinstance(results, list) and results
    assert results[0]["job_result_id"] == oldRevision
    assert results[0]["content"] == "工程验收计划"
    assert results[0]["source"]["job_result_id"] == oldRevision
    assert results[0]["source"]["source_file_name"] == "report.pdf"
    assert cleanup.collect(oldBuild)


def test_classic_read_reopens_a_closed_index_through_normal_admission(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.orm import sessionmaker
    from shared.core.config import settings
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    engine, namespace = classicRuntime
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_IDLE_HIGH_WATERMARK", 1)
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_IDLE_LOW_WATERMARK", 0)
    index = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    try:
        assert LexicalCoordination(sessionmaker(engine, expire_on_commit=False)).trimIdle(index) == 1
        assert asyncio.run(_queryClassic(namespace))["results"]
    finally:
        index.close()


@pytest.mark.parametrize("hasKnownFailure", [False, True])
def test_classic_unknown_native_outcome_is_unavailable_without_reader_leases(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch, hasKnownFailure: bool,
) -> None:
    from shared.models.database.lexicallease import LexicalLease
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
    from shared.core.exceptions.domain_exceptions import UnavailableException

    engine, namespace = classicRuntime
    original = AsyncOpenSearchLexicalIndex._request

    async def loseResponse(self: AsyncOpenSearchLexicalIndex, method: str, path: str, *,
                     payload: object | None = None, textBody: str | None = None,
                     operationId: str | None = None, timeoutSeconds: float | None = None) -> object:
        if "/_search?" in path:
            if hasKnownFailure and isinstance(payload, dict) and "pathTokens" in str(payload.get("query")):
                raise LexicalIndexError("Injected known channel refusal", isRetryable=True)
            raise LexicalIndexError("Injected lost native response", isRetryable=True, hasUnknownOutcome=True)
        return await original(self, method, path, payload=payload, textBody=textBody,
            operationId=operationId, timeoutSeconds=timeoutSeconds)

    monkeypatch.setattr(AsyncOpenSearchLexicalIndex, "_request", loseResponse)
    with pytest.raises(UnavailableException):
        asyncio.run(_queryClassic(namespace))
    with Session(engine) as database:
        assert not list(database.scalars(select(LexicalLease).where(LexicalLease.kind == "revision_read")))
        assert not list(database.scalars(select(LexicalLease).where(LexicalLease.state == "active")))


def test_classic_finalization_reuses_the_body_database_session(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import event
    from shared.core.database_sync import get_sync_engine
    from shared.services.retrieval.execution import routes

    _, namespace = classicRuntime
    syncEngine: Engine = get_sync_engine()
    checkouts: list[bool] = []
    watching: list[bool] = []

    def observeCheckout(connection: object, record: object, proxy: object) -> None:
        checkouts.append(True)

    def beginWatching() -> None:
        if not watching:
            event.listen(syncEngine, "checkout", observeCheckout)
            watching.append(True)

    monkeypatch.setattr(routes, "assemble_retrieval_results", _inspectAsyncCall(routes.assemble_retrieval_results, beginWatching))
    try:
        assert asyncio.run(_queryClassic(namespace))["results"]
        assert watching and not checkouts, "Finalization checked out another sync connection after body assembly"
    finally:
        if watching:
            event.remove(syncEngine, "checkout", observeCheckout)


def test_classic_hydration_and_finalization_share_one_transaction(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import event
    from sqlalchemy.engine import Connection
    from shared.services.retrieval.lexical.hit import LexicalHit
    from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex

    _, namespace = classicRuntime
    completedNative: list[bool] = []
    commits: list[bool] = []
    original = AsyncOpenSearchLexicalIndex.search

    async def finishNative(self: AsyncOpenSearchLexicalIndex, *, buildIds: tuple[str, ...],
                     queryTokens: tuple[str, ...], limit: int,
                     unitIds: tuple[str, ...] | None = None) -> tuple[LexicalHit, ...]:
        hits: tuple[LexicalHit, ...] = await original(self, buildIds=buildIds, queryTokens=queryTokens,
            limit=limit, unitIds=unitIds)
        completedNative.append(True)
        return hits

    def observeCommit(connection: Connection) -> None:
        if completedNative and connection.engine.url.drivername == "postgresql+asyncpg":
            commits.append(True)

    monkeypatch.setattr(AsyncOpenSearchLexicalIndex, "search", finishNative)
    event.listen(Engine, "commit", observeCommit)
    try:
        assert asyncio.run(_queryClassic(namespace))["results"]
        assert len(commits) == 1, "Body hydration committed before the final read validation"
    finally:
        event.remove(Engine, "commit", observeCommit)


def test_api_retrieval_uses_its_existing_async_database_engine(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from shared.services.retrieval.execution.plan import run_retrieval_query
    from shared.services.retrieval.lexical import reader

    _, namespace = classicRuntime

    def rejectSeparatePool() -> None:
        raise AssertionError("API retrieval opened a separate synchronous database pool")

    monkeypatch.setattr(reader, "get_sync_session_factory", rejectSeparatePool)

    async def exercise() -> None:
        database = create_async_engine(get_contract_database_url(), pool_size=2, max_overflow=0)
        observed: list[bool] = []

        def inspectConnection(connection: object, *_arguments: object) -> None:
            assert getattr(connection, "engine") is database.sync_engine
            observed.append(True)

        event.listen(Engine, "before_cursor_execute", inspectConnection)
        try:
            async with AsyncSession(database) as session:
                response = await run_retrieval_query(db=session, user_id="native-contract-user", namespace=namespace,
                    query="工程验收计划", top_k=1, use_agentic=False, exclude_document_ids=[], exclude_sections=[])
                assert response["results"]
            assert observed
        finally:
            event.remove(Engine, "before_cursor_execute", inspectConnection)
            from shared.services.retrieval.lexical.asyncruntime import AsyncOpenSearchRuntime
            await AsyncOpenSearchRuntime.closeCurrent()
            await database.dispose()

    asyncio.run(exercise())


def test_api_native_retrieval_does_not_submit_to_a_thread_executor(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, namespace = classicRuntime

    async def rejectExecutor(*_arguments: object, **_options: object) -> None:
        raise AssertionError("API native retrieval submitted blocking work to a thread executor")

    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "to_thread", rejectExecutor)
        assert asyncio.run(_queryClassic(namespace))["results"]


def test_api_native_retrieval_uses_async_http(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, namespace = classicRuntime

    def rejectSyncHttp(*_arguments: object, **_options: object) -> None:
        raise AssertionError("API native retrieval issued synchronous HTTP")

    with monkeypatch.context() as patch:
        patch.setattr(httpx.Client, "request", rejectSyncHttp)
        assert asyncio.run(_queryClassic(namespace))["results"]


def test_async_native_handles_reuse_http_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if endpoint is None:
        pytest.skip("An owned OpenSearch node is required")
    from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
    from shared.services.retrieval.lexical.asyncruntime import AsyncOpenSearchRuntime
    from shared.services.retrieval.lexical.scope import LexicalScope

    streams: list[object] = []

    def inspectResponses(original: Callable[_Parameters, Awaitable[httpx.Response]]) -> Callable[_Parameters, Awaitable[httpx.Response]]:
        async def inspected(*arguments: _Parameters.args, **options: _Parameters.kwargs) -> httpx.Response:
            response: httpx.Response = await original(*arguments, **options)
            streams.append(response.extensions.get("network_stream"))
            return response
        return inspected

    async def exercise() -> None:
        scope = LexicalScope(ownerId="transport-contract", namespace="async-transport")
        first = AsyncOpenSearchLexicalIndex(endpoint=endpoint, scope=scope)
        second = AsyncOpenSearchLexicalIndex(endpoint=endpoint, scope=scope)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(httpx.AsyncClient, "send", inspectResponses(httpx.AsyncClient.send))
                await first._request("GET", "/")
                first.close()
                await second._request("GET", "/")
            assert len(streams) == 2 and streams[0] is not None
            assert streams[0] is streams[1]
        finally:
            first.close()
            second.close()
            await AsyncOpenSearchRuntime.closeCurrent()

    asyncio.run(exercise())


def test_async_cancelled_channel_keeps_uncertainty_after_a_known_sibling_failure(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core.exceptions.domain_exceptions import UnavailableException
    from shared.models.database.lexicallease import LexicalLease
    from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
    from shared.services.retrieval.lexical.error import LexicalIndexError

    engine, namespace = classicRuntime
    original = AsyncOpenSearchLexicalIndex._request

    async def cancelSibling(self: AsyncOpenSearchLexicalIndex, method: str, path: str, *,
                            payload: object | None = None, textBody: str | None = None,
                            operationId: str | None = None, timeoutSeconds: float | None = None) -> object:
        if "/_search?" in path:
            if isinstance(payload, dict) and "pathTokens" in str(payload.get("query")):
                raise LexicalIndexError("Injected known channel refusal", isRetryable=True)
            raise asyncio.CancelledError
        return await original(self, method, path, payload=payload, textBody=textBody,
            operationId=operationId, timeoutSeconds=timeoutSeconds)

    monkeypatch.setattr(AsyncOpenSearchLexicalIndex, "_request", cancelSibling)
    with pytest.raises(UnavailableException):
        asyncio.run(_queryClassic(namespace))
    with Session(engine) as database:
        assert not list(database.scalars(select(LexicalLease).where(LexicalLease.kind == "revision_read")))


def test_activation_reuses_its_locked_namespace_generation(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    _getNativeEndpoint()
    from sqlalchemy import event
    from sqlalchemy.engine import Connection, ExecutionContext
    from sqlalchemy.orm import sessionmaker

    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc,
        webhookEvents=["job.completed"])
    from shared.core.config import settings
    from shared.models.database.document import Document, RetrievalNamespaceGeneration
    from shared.models.database.job import Job
    from shared.models.database.webhook import WebhookEvent
    from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.worker import IndexingWorker, _Claim

    index = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    original = IndexingWorker._activate
    lockedGenerations: list[str] = []
    notifications: list[str] = []

    def inspectNotification(eventId: str | None) -> None:
        if eventId is not None:
            notifications.append(eventId)

    monkeypatch.setattr(SyncJobWebhookOutbox, "enqueue_event_id_after_commit", staticmethod(inspectNotification))

    def inspectStatement(connection: Connection, cursor: object, statement: str,
                         parameters: object, context: ExecutionContext, many: bool) -> None:
        normalized: str = statement.lower()
        if "retrieval_namespace_generations" in normalized and "for update" in normalized:
            lockedGenerations.append(statement)

    def inspectActivation(self: IndexingWorker, claim: _Claim) -> str:
        event.listen(engine, "after_cursor_execute", inspectStatement)
        try:
            return original(self, claim)
        finally:
            event.remove(engine, "after_cursor_execute", inspectStatement)

    monkeypatch.setattr(IndexingWorker, "_activate", inspectActivation)
    try:
        assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId) == "completed"
        assert len(lockedGenerations) == 1, (
            f"Activation acquired the same generation row {len(lockedGenerations)} times"
        )
        with Session(engine) as database:
            document = database.get(Document, "doc_native_contract")
            job = database.get(Job, jobId)
            generation = database.scalars(select(RetrievalNamespaceGeneration).where(
                RetrievalNamespaceGeneration.namespace == namespace)).one()
            notification = database.scalars(select(WebhookEvent).where(WebhookEvent.job_id == jobId)).one()
            assert document is not None and document.current_indexing_build_id is not None
            assert job is not None and job.status == "done"
            assert generation.generation == 1
            assert notification.payload["event"] == "job.completed"
            assert notifications == [notification.id]
    finally:
        response: httpx.Response = httpx.delete(
            f"{settings.KNOWHERE_OPENSEARCH_URL}/{index.indexName}", timeout=15, trust_env=False
        )
        assert response.status_code in (200, 404)
        index.close()
        engine.dispose()


def test_query_does_not_wait_for_shared_coordination_rows(
    classicRuntime: tuple[Engine, str],
) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.serving_generation import lock_namespace_generation

    engine, namespace = classicRuntime
    with Session(engine) as database, database.begin():
        LexicalCoordination.lockControl(database, isExclusive=True)
        lock_namespace_generation(database, user_id="native-contract-user", namespace=namespace)
        database.scalars(select(LexicalNamespaceIndex).with_for_update()).all()
        response: dict[str, object] = asyncio.run(asyncio.wait_for(_queryClassic(namespace), timeout=3))
    assert response["results"]


def test_query_returns_empty_without_restart_when_its_old_build_is_cleaned(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.orm import sessionmaker
    from shared.models.database.document import Document
    from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
    from shared.services.retrieval.lexical.cleanup import LexicalCleanup
    from shared.services.retrieval.lexical.hit import LexicalHit
    from shared.services.retrieval.lexical.worker import IndexingWorker

    engine, namespace = classicRuntime
    factory = sessionmaker(engine, expire_on_commit=False)
    with Session(engine) as database:
        oldBuild: str | None = database.scalars(select(Document.current_indexing_build_id)).one()
    assert oldBuild is not None
    original = AsyncOpenSearchLexicalIndex.search
    calls: list[tuple[str, ...]] = []

    async def replaceBeforeSearch(self: AsyncOpenSearchLexicalIndex, *, buildIds: tuple[str, ...],
                                 queryTokens: tuple[str, ...], limit: int,
                                 unitIds: tuple[str, ...] | None = None) -> tuple[LexicalHit, ...]:
        calls.append(buildIds)
        if len(calls) == 1:
            jobId: str = _stageReplacement(engine, namespace)
            assert IndexingWorker(sessionFactory=factory).run(jobId) == "completed"
            assert LexicalCleanup(sessionFactory=factory).collect(oldBuild)
        return await original(self, buildIds=buildIds, queryTokens=queryTokens, limit=limit, unitIds=unitIds)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncOpenSearchLexicalIndex, "search", replaceBeforeSearch)
        response: dict[str, object] = asyncio.run(_queryClassic(namespace))
    assert response["results"] == []
    assert calls == [(oldBuild,)]
    assert asyncio.run(_queryClassic(namespace))["results"]


def test_query_recovers_once_when_lru_closes_between_capture_and_search(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.orm import sessionmaker
    from shared.core.config import settings
    from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.hit import LexicalHit
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    engine, namespace = classicRuntime
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_IDLE_HIGH_WATERMARK", 1)
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_IDLE_LOW_WATERMARK", 0)
    original = AsyncOpenSearchLexicalIndex.search
    calls: list[tuple[str, ...]] = []
    index = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")

    async def closeBeforeSearch(self: AsyncOpenSearchLexicalIndex, *, buildIds: tuple[str, ...],
                               queryTokens: tuple[str, ...], limit: int,
                               unitIds: tuple[str, ...] | None = None) -> tuple[LexicalHit, ...]:
        calls.append(buildIds)
        if len(calls) == 1:
            assert LexicalCoordination(sessionmaker(engine, expire_on_commit=False)).trimIdle(index) == 1
        return await original(self, buildIds=buildIds, queryTokens=queryTokens, limit=limit, unitIds=unitIds)

    try:
        monkeypatch.setattr(AsyncOpenSearchLexicalIndex, "search", closeBeforeSearch)
        assert asyncio.run(_queryClassic(namespace))["results"]
        assert len(calls) == 2 and calls[0] == calls[1]
    finally:
        index.close()


def test_writer_fails_once_after_lru_closure_and_reopens_only_on_manual_retry(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    from sqlalchemy.orm import sessionmaker
    _getNativeEndpoint()

    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc)
    from shared.core.config import settings
    from shared.models.database.indexing import IndexingAttempt
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.worker import IndexingWorker

    original = OpenSearchLexicalIndex._request
    batches: list[str] = []
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_IDLE_HIGH_WATERMARK", 1)
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_IDLE_LOW_WATERMARK", 0)
    index = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")

    def closeAfterBatch(self: OpenSearchLexicalIndex, method: str, path: str, *,
                        payload: object | None = None, textBody: str | None = None,
                        operationId: str | None = None, timeoutSeconds: float | None = None) -> object:
        response: object = original(self, method, path, payload=payload, textBody=textBody,
            operationId=operationId, timeoutSeconds=timeoutSeconds)
        if "/_bulk?" in path:
            assert textBody is not None
            batches.append(textBody)
            if len(batches) == 1:
                assert LexicalCoordination(sessionmaker(engine, expire_on_commit=False)).trimIdle(self) == 1
        return response

    try:
        monkeypatch.setattr(OpenSearchLexicalIndex, "_request", closeAfterBatch)
        factory = sessionmaker(engine, expire_on_commit=False)
        assert IndexingWorker(sessionFactory=factory).run(jobId) == "failed"
        assert IndexingWorker(sessionFactory=factory).run(jobId) == "failed"
        assert len(batches) == 1
        from shared.services.retrieval.lexical.retry import IndexingRetry
        with Session(engine) as database:
            first = database.scalars(select(IndexingAttempt)).one()
            assert first.executionCount == 1 and first.state == "failed"
            assert IndexingRetry().request(database, jobId=jobId, userId="native-contract-user")
            database.commit()
        assert IndexingWorker(sessionFactory=factory).run(jobId) == "completed"
        assert len(batches) == 2 and batches[0] != batches[1]
        with Session(engine) as database:
            attempts = list(database.scalars(select(IndexingAttempt).order_by(IndexingAttempt.attemptNumber)))
            assert [attempt.executionCount for attempt in attempts] == [1, 1]
    finally:
        response: httpx.Response = httpx.delete(
            f"{settings.KNOWHERE_OPENSEARCH_URL}/{index.indexName}", timeout=10, trust_env=False
        )
        assert response.status_code in (200, 404)
        index.close()
        engine.dispose()


def test_query_stops_after_one_lru_recovery_without_returning_empty_success(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.orm import sessionmaker
    from shared.core.config import settings
    from shared.core.exceptions.domain_exceptions import UnavailableException
    from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.hit import LexicalHit
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    engine, namespace = classicRuntime
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_IDLE_HIGH_WATERMARK", 1)
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_IDLE_LOW_WATERMARK", 0)
    original = AsyncOpenSearchLexicalIndex.search
    calls: list[tuple[str, ...]] = []
    index = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")

    async def closeEverySearch(self: AsyncOpenSearchLexicalIndex, *, buildIds: tuple[str, ...],
                              queryTokens: tuple[str, ...], limit: int,
                              unitIds: tuple[str, ...] | None = None) -> tuple[LexicalHit, ...]:
        calls.append(buildIds)
        assert LexicalCoordination(sessionmaker(engine, expire_on_commit=False)).trimIdle(index) == 1
        return await original(self, buildIds=buildIds, queryTokens=queryTokens, limit=limit, unitIds=unitIds)

    try:
        monkeypatch.setattr(AsyncOpenSearchLexicalIndex, "search", closeEverySearch)
        with pytest.raises(UnavailableException):
            asyncio.run(_queryClassic(namespace))
        assert len(calls) == 2 and calls[0] == calls[1]
    finally:
        index.close()


def test_missing_native_index_is_not_recreated_or_returned_as_empty(
    classicRuntime: tuple[Engine, str],
) -> None:
    from shared.core.config import settings
    from shared.core.exceptions.domain_exceptions import UnavailableException
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    _, namespace = classicRuntime
    index = OpenSearchLexicalIndex(endpoint=settings.KNOWHERE_OPENSEARCH_URL,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace), indexPrefix="knowhere-slice-test")
    try:
        response: httpx.Response = httpx.delete(
            f"{settings.KNOWHERE_OPENSEARCH_URL}/{index.indexName}", timeout=10, trust_env=False
        )
        assert response.status_code == 200
        with pytest.raises(UnavailableException):
            asyncio.run(_queryClassic(namespace))
        assert httpx.head(f"{settings.KNOWHERE_OPENSEARCH_URL}/{index.indexName}",
            timeout=10, trust_env=False).status_code == 404
    finally:
        index.close()


def test_document_filtered_search_prefers_distinct_query_terms_to_repetition(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str = _getNativeEndpoint()
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    query: str = "battery voltage cold shutdown temperature capacity"
    content: list[str] = [
        query + " spacer" * 900,
        "battery voltage " * 10,
        *(f"cold shutdown temperature capacity note {position}" for position in range(12)),
    ]
    chunks: list[dict[str, object]] = [
        {"chunk_id": f"coverage-{position}", "type": "text", "content": text,
         "path": f"report.pdf/Section {position}", "metadata": {}}
        for position, text in enumerate(content)
    ]
    engine, jobId, namespace = _prepareParsedJob(
        monkeypatch, postgresql_proc, preparedChunks=chunks,
    )
    from shared.services.retrieval.execution.plan import run_retrieval_query
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.worker import IndexingWorker

    monkeypatch.setattr(LegacyCopyDispatch, "submit", staticmethod(lambda _identity: False))
    native: OpenSearchLexicalIndex = OpenSearchLexicalIndex(
        endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace),
        indexPrefix="knowhere-slice-test",
    )

    async def getFirstChunk(include: list[str] | None) -> str:
        asyncEngine = create_async_engine(get_contract_database_url())
        try:
            async with AsyncSession(asyncEngine) as session:
                response = await run_retrieval_query(
                    db=session, user_id="native-contract-user", namespace=namespace,
                    query=query, top_k=1, include_document_ids=include, use_agentic=False,
                    exclude_document_ids=[], exclude_sections=[],
                )
                assert response["router_used"] == "classic_topk"
                return str(response["results"][0]["chunk_id"])
        finally:
            await asyncEngine.dispose()

    try:
        assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId) == "completed"
        assert asyncio.run(getFirstChunk(None)) == "coverage-1"
        assert asyncio.run(getFirstChunk(["doc_native_contract"])) == "coverage-0"
    finally:
        response: httpx.Response = httpx.delete(f"{endpoint}/{native.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        native.close()
        engine.dispose()
