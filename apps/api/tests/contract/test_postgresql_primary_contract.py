"""PG primary consumes frozen input and publishes only under the common fence."""

from __future__ import annotations
from typing import TypeVar, cast
import asyncio
from collections.abc import Iterator
from datetime import timedelta
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
def postgresqlPrimaryRuntime(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess
) -> Iterator[tuple[Engine, str, str]]:
    configure_contract_environment(monkeypatch, postgresql_proc)
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "postgresql")
    clear_application_modules()
    asyncio.run(prepare_contract_storage())
    from shared.models.database.job import Job
    from shared.models.database.user import User
    from shared.services.retrieval.lexical.parsing import ParsedRevisionFinalizer
    from shared.models.database.indexing import IndexingAttempt

    engine: Engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    identity: str = uuid4().hex[:10]
    jobId: str = f"pg-{identity}"
    ownerToken: str = str(uuid4())
    with Session(engine) as db:
        db.add(
            User(
                id=f"user-{identity}",
                name="PG contract",
                email=f"{identity}@example.invalid",
            )
        )
        db.flush()
        db.add(
            Job(
                job_id=jobId,
                user_id=f"user-{identity}",
                job_type="document_ingestion",
                source_type="file",
                status="running",
                credits_charged=123456,
                billing_status="charged",
                webhook_enabled=True,
                webhook_url="https://example.invalid/hook",
                webhook_events=["job.parsed", "job.completed", "job.failed"],
                job_metadata={
                    "document_id": f"doc_{identity}",
                    "namespace": f"pg-{identity}",
                    "source_file_name": "report.pdf",
                },
            )
        )
        db.commit()
        ParsedRevisionFinalizer().finalize(
            db,
            jobId=jobId,
            resultKey=f"results/{identity}.zip",
            checksum="checksum",
            resultSize=100,
            chunks=[
                {
                    "chunk_id": "chunk-1",
                    "type": "text",
                    "content": "frozen evidence 工程",
                    "path": "report.pdf/工程",
                    "metadata": {},
                }
            ],
            storedCount=1,
            deliveryMode="url",
            sectionSummaries=None,
            documentTopSummary=None,
        )
        db.commit()
        attempt: IndexingAttempt = db.scalars(
            select(IndexingAttempt).where(IndexingAttempt.jobId == jobId)
        ).one()
        now = db.scalars(select(func.clock_timestamp())).one()
        attempt.state = "running"
        attempt.executionCount = 1
        attempt.ownerToken = ownerToken
        attempt.startedAt = now
        attempt.executionDeadlineAt = now + timedelta(seconds=300)
        attempt.claimExpiresAt = attempt.executionDeadlineAt
        db.commit()
    try:
        yield (engine, jobId, ownerToken)
    finally:
        engine.dispose()


def getContext(engine: Engine, jobId: str):
    from shared.services.retrieval.lexical.worker import IndexingWorker

    worker = IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False))
    context = worker._readContext(jobId)
    assert not isinstance(context, str)
    return context


def test_pg_primary_completes_same_job_without_native_dependency(
    postgresqlPrimaryRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.models.database.document import (
        Document,
        DocumentMapUnitIndex,
        DocumentMapUnitToken,
    )
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.webhook import WebhookEvent
    from shared.services.retrieval.lexical.deadline import IndexingDeadline
    from shared.services.retrieval.lexical.pgpublication import PostgresqlPublication
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex

    def refuseNative(*args, **kwargs):
        raise AssertionError("PG primary cannot instantiate a native client")

    monkeypatch.setattr(OpenSearchLexicalIndex, "__init__", refuseNative)
    engine, jobId, ownerToken = postgresqlPrimaryRuntime
    context = getContext(engine, jobId)
    with Session(engine) as db:
        result = db.scalars(select(JobResult).where(JobResult.job_id == jobId)).one()
        originalResultId = result.id
        assert (
            _require(db.get(Document, context.documentId)).current_job_result_id is None
        )
    outcome = PostgresqlPublication(
        sessionFactory=sessionmaker(engine, expire_on_commit=False),
        deadline=IndexingDeadline(300),
    ).run(context, ownerToken=ownerToken)
    assert outcome.eventId
    with Session(engine) as db:
        job: Job = _require(db.get(Job, jobId))
        attempt: IndexingAttempt = _require(db.get(IndexingAttempt, context.attemptId))
        result = _require(db.get(JobResult, originalResultId))
        assert (
            job.status == "done"
            and job.parsing_status == "completed"
            and (job.indexing_status == "completed")
        )
        assert job.credits_charged == 123456 and job.billing_status == "charged"
        assert attempt.executionCount == 1 and attempt.state == "succeeded"
        assert result.indexing_input_key and result.indexing_input_checksum
        assert (
            _require(db.get(Document, context.documentId)).current_job_result_id
            == originalResultId
        )
        assert (
            _require(db.get(Document, context.documentId)).current_indexing_build_id
            is None
        )
        marker = db.scalars(
            select(DocumentMapUnitIndex).where(
                DocumentMapUnitIndex.job_result_id == originalResultId
            )
        ).one()
        assert marker.token_count > 0 and marker.unit_count > 0
        assert (
            db.scalar(select(func.count()).select_from(DocumentMapUnitToken))
            == marker.token_count
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(WebhookEvent)
                .where(WebhookEvent.payload["event"].astext == "job.completed")
            )
            == 1
        )


def test_pg_primary_binds_demo_examples_to_the_published_revision(
    postgresqlPrimaryRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.core.config import settings
    from shared.models.database.demo_corpus import DemoDocument
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
    from shared.services.retrieval.corpus_storage import DEMO_NAMESPACE, DEMO_OWNER
    from shared.services.retrieval.lexical.worker import IndexingWorker

    engine, initial_job_id, _ = postgresqlPrimaryRuntime
    job_id: str = "pg-demo-binding"
    document_id: str = "ddoc_pg_example_binding"
    with Session(engine) as db:
        maintainer_id: str = _require(db.get(Job, initial_job_id)).user_id
        monkeypatch.setattr(settings, "DEMO_MAINTAINER_USER_IDS", maintainer_id)
        db.add(
            DemoDocument(
                document_id=document_id,
                demo_source_id="pg-demo-example",
                title="Demo example",
                user_id=DEMO_OWNER,
                namespace=DEMO_NAMESPACE,
                catalog_metadata={
                    "examples": [
                        {
                            "id": "example-1",
                            "question": "What is the finding?",
                            "answer": "Unique evidence.",
                            "citations": [{"content": "Unique evidence"}],
                        }
                    ]
                },
            )
        )
        db.add(
            Job(
                job_id=job_id,
                user_id=maintainer_id,
                job_type="document_ingestion",
                source_type="file",
                status="running",
                job_metadata={
                    "document_id": document_id,
                    "namespace": DEMO_NAMESPACE,
                    "corpus_target": "DEMO",
                    "demo_source_id": "pg-demo-example",
                    "source_file_name": "demo.pdf",
                },
            )
        )
        db.commit()
        finalization = SyncJobSuccessFinalizer().finalize(
            db,
            job_id=job_id,
            result_s3_key="results/pg-demo-binding.zip",
            checksum="checksum",
            zip_size=10,
            chunks=[
                {
                    "chunk_id": "demo-citation",
                    "type": "text",
                    "content": "Unique evidence.",
                    "path": "demo.pdf/Findings",
                    "metadata": {"page_nums": [1]},
                }
            ],
            stored_count=1,
            delivery_mode="url",
            section_summaries=None,
        )
        assert finalization.response.should_commit()
        db.commit()

    assert IndexingWorker(sessionFactory=sessionmaker(engine)).run(job_id) == "completed"
    with Session(engine) as db:
        document: DemoDocument = _require(db.get(DemoDocument, document_id))
        result: JobResult = _require(db.get(JobResult, document.current_job_result_id))
        bound_examples = (result.document_metadata or {})["bound_demo_examples"]
        assert [example["id"] for example in bound_examples] == ["example-1"]
        citation = bound_examples[0]["citations"][0]
        assert citation["job_result_id"] == result.id
        assert citation["chunk_id"] == "demo-citation"
        assert citation["page_number"] == 1


def test_pg_primary_reuses_assigned_tokens_without_current_compiler(
    postgresqlPrimaryRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.models.database.document import DocumentMapUnitToken
    from shared.models.database.job_result import JobResult
    from shared.services.retrieval.lexical.deadline import IndexingDeadline
    from shared.services.retrieval.lexical.input import LexicalInput
    from shared.services.retrieval.lexical.unit import LexicalUnit
    from shared.services.retrieval.lexical.storage import LexicalInputStorage
    from shared.services.retrieval.lexical.preparation import LexicalPreparation
    from shared.services.retrieval.lexical.pgpublication import PostgresqlPublication

    engine, jobId, ownerToken = postgresqlPrimaryRuntime
    context = getContext(engine, jobId)
    with Session(engine) as db:
        from shared.models.database.document import DocumentSection

        sectionId = _require(
            db.scalars(
                select(DocumentSection.section_id).where(
                    DocumentSection.job_result_id == context.revisionId
                )
            ).first()
        )
    payload = LexicalInput(
        compilerFingerprint="frozen-old-compiler",
        units=(
            LexicalUnit(
                documentId=context.documentId,
                revisionId=context.revisionId,
                unitId="unit-frozen",
                sectionId=sectionId,
                pathTokens="frozenpath frozenpath",
                contentTokens="frozenword",
            ),
        ),
    )
    key, checksum, size = LexicalInputStorage().saveWithSize(
        jobId=jobId, revisionId=context.revisionId, payload=payload
    )
    with Session(engine) as db:
        result = _require(db.get(JobResult, context.revisionId))
        result.indexing_input_key = key
        result.indexing_input_checksum = checksum
        result.indexing_input_compiler = payload.compilerFingerprint
        result.indexing_input_bytes = size
        db.commit()

    def refusePreparation(**kwargs):
        raise AssertionError("Assigned frozen input cannot be retokenized")

    monkeypatch.setattr(LexicalPreparation, "prepare", refusePreparation)
    PostgresqlPublication(
        sessionFactory=sessionmaker(engine, expire_on_commit=False),
        deadline=IndexingDeadline(300),
    ).run(context, ownerToken=ownerToken)
    with Session(engine) as db:
        tokens = {
            (row.channel, row.token, row.frequency)
            for row in db.scalars(select(DocumentMapUnitToken))
        }
        assert tokens == {("path", "frozenpath", 2), ("content", "frozenword", 1)}


def test_pg_primary_fences_stale_owner_and_maintenance(
    postgresqlPrimaryRuntime,
) -> None:
    from shared.models.database.document import Document
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.lexical.deadline import IndexingDeadline
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.pgpublication import PostgresqlPublication

    engine, jobId, ownerToken = postgresqlPrimaryRuntime
    context = getContext(engine, jobId)
    adapter = PostgresqlPublication(
        sessionFactory=sessionmaker(engine, expire_on_commit=False),
        deadline=IndexingDeadline(300),
    )
    with pytest.raises(LexicalIndexError):
        adapter.run(context, ownerToken="stale-owner")
    with Session(engine) as db:
        control = _require(db.get(LexicalBackendControl, 1))
        control.mode = "gated"
        db.commit()
    with pytest.raises(LexicalIndexError):
        adapter.run(context, ownerToken=ownerToken)
    with Session(engine) as db:
        assert (
            _require(db.get(Document, context.documentId)).current_job_result_id is None
        )


def test_async_pg_primary_uses_async_input_and_sql_bridge(
    postgresqlPrimaryRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from shared.services.retrieval.lexical.asyncdatabase import AsyncLexicalDatabase
    from shared.services.retrieval.lexical.deadline import IndexingDeadline
    from shared.services.retrieval.lexical.pgpublication import PostgresqlPublication
    from shared.services.retrieval.lexical.storage import LexicalInputStorage

    engine, jobId, ownerToken = postgresqlPrimaryRuntime
    context = getContext(engine, jobId)

    def refuseSyncIO(*args, **kwargs):
        raise AssertionError("Async PG publication cannot call sync object storage")

    monkeypatch.setattr(LexicalInputStorage, "load", refuseSyncIO)
    monkeypatch.setattr(LexicalInputStorage, "saveWithSize", refuseSyncIO)

    async def run():
        asyncEngine = create_async_engine(
            engine.url.set(drivername="postgresql+asyncpg")
        )
        database = AsyncLexicalDatabase(asyncEngine)
        try:
            adapter = PostgresqlPublication(
                sessionFactory=database.sessions, deadline=IndexingDeadline(300)
            )
            outcome = await adapter.runAsync(
                context, ownerToken=ownerToken, database=database
            )
            assert outcome.eventId
        finally:
            await asyncEngine.dispose()

    asyncio.run(run())


def test_slow_pg_copy_holds_no_control_or_namespace_publication_lock(
    postgresqlPrimaryRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from shared.models.database.document import Document
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.lexical import pgpublication
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.deadline import IndexingDeadline
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.serving_generation import lock_namespace_generation

    engine, jobId, ownerToken = postgresqlPrimaryRuntime
    context = getContext(engine, jobId)
    entered: Event = Event()
    release: Event = Event()
    copy = pgpublication.insert_token_records_with_copy

    def pauseCopy(db, rows, *, corpus_storage):
        entered.set()
        assert release.wait(3)
        copy(db, rows, corpus_storage=corpus_storage)

    monkeypatch.setattr(pgpublication, "insert_token_records_with_copy", pauseCopy)
    adapter = pgpublication.PostgresqlPublication(
        sessionFactory=sessionmaker(engine, expire_on_commit=False),
        deadline=IndexingDeadline(300),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(adapter.run, context, ownerToken=ownerToken)
        assert entered.wait(3)
        try:
            with Session(engine) as db:
                db.execute(select(func.set_config("lock_timeout", "300ms", True)))
                control: LexicalBackendControl = LexicalCoordination.lockControl(
                    db, isExclusive=True
                )
                lock_namespace_generation(
                    db, user_id=context.scope.ownerId, namespace=context.scope.namespace
                )
                assert (
                    _require(db.get(Document, context.documentId)).current_job_result_id
                    is None
                )
                control.mode = "gated"
                control.epoch += 1
                db.commit()
        finally:
            release.set()
        with pytest.raises(LexicalIndexError):
            future.result(timeout=3)
    with Session(engine) as db:
        assert (
            _require(db.get(Document, context.documentId)).current_job_result_id is None
        )


_Value = TypeVar("_Value")


def _require(value: _Value | None) -> _Value:
    assert value is not None
    return value


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_async_pg_primary_uses_cooperative_preparation_api(
    postgresqlPrimaryRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from shared.services.retrieval.lexical.asyncdatabase import AsyncLexicalDatabase
    from shared.services.retrieval.lexical.deadline import IndexingDeadline
    from shared.services.retrieval.lexical.pgpublication import PostgresqlPublication
    from shared.services.retrieval.lexical.preparation import LexicalPreparation

    engine, jobId, ownerToken = postgresqlPrimaryRuntime
    context = getContext(engine, jobId)
    prepare = LexicalPreparation.prepare
    calls: list[str] = []

    async def prepareAsync(**options):
        deadline = options.pop("deadline")
        assert deadline is not None
        calls.append("cooperative")
        return prepare(**options)

    def refuseSyncPreparation(**options):
        raise AssertionError(
            "Async PG execution called blocking whole-document preparation"
        )

    monkeypatch.setattr(LexicalPreparation, "prepareAsync", prepareAsync, raising=False)
    monkeypatch.setattr(LexicalPreparation, "prepare", refuseSyncPreparation)

    async def run() -> None:
        asyncEngine = create_async_engine(
            engine.url.set(drivername="postgresql+asyncpg")
        )
        database = AsyncLexicalDatabase(asyncEngine)
        try:
            adapter = PostgresqlPublication(
                sessionFactory=database.sessions, deadline=IndexingDeadline(300)
            )
            await adapter.runAsync(context, ownerToken=ownerToken, database=database)
        finally:
            await asyncEngine.dispose()

    asyncio.run(run())
    assert calls == ["cooperative"]


def test_sync_pg_primary_preparation_checks_deadline_and_yields(
    postgresqlPrimaryRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gevent
    from shared.services.retrieval.lexical.deadline import IndexingDeadline
    from shared.services.retrieval.lexical.pgpublication import PostgresqlPublication
    from shared.services.retrieval.lexical.preparation import LexicalPreparation

    engine, jobId, ownerToken = postgresqlPrimaryRuntime
    context = getContext(engine, jobId)
    prepare = LexicalPreparation.prepare
    calls: list[float] = []

    def prepareWithCheckpoints(**options):
        assert options.get("checkpoint") is not None
        return prepare(**options)

    monkeypatch.setattr(LexicalPreparation, "prepare", prepareWithCheckpoints)
    monkeypatch.setattr(gevent, "sleep", lambda seconds: calls.append(seconds))
    adapter = PostgresqlPublication(
        sessionFactory=sessionmaker(engine, expire_on_commit=False),
        deadline=IndexingDeadline(300),
    )
    adapter.run(context, ownerToken=ownerToken)
    assert calls and set(calls) == {0}


@pytest.mark.asyncio
async def test_context_only_job_read_retains_authorized_parse_without_signing(
    postgresqlPrimaryRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.services.jobs.read_service import get_job_result_for_user
    from shared.core.exceptions.domain_exceptions import PermissionDeniedException
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.services.storage.job_file_storage import JobFileStorage

    engine, jobId, _ = postgresqlPrimaryRuntime
    with Session(engine) as db:
        job = _require(db.get(Job, jobId))
        userId: str = job.user_id
        revision = db.scalars(select(JobResult).where(JobResult.job_id == jobId)).one()
        revision.inline_payload = {"chunks": [{"content": "retained parse"}]}
        revisionId: str = revision.id
        db.commit()

    async def refuseSigning(*arguments: object, **options: object) -> None:
        raise AssertionError("Context-only projection attempted URL signing")

    monkeypatch.setattr(JobFileStorage, "generateDownloadUrlAsync", refuseSigning)
    asyncEngine = create_async_engine(engine.url.set(drivername="postgresql+asyncpg"))
    try:
        async with AsyncSession(asyncEngine) as db:
            retained = await get_job_result_for_user(
                db, job_id=jobId, user_id=userId, shouldSignResultURL=False
            )
            assert retained.job_id == jobId and retained.job_result_id == revisionId
            assert (
                retained.parsing_status == "completed"
                and retained.indexing_status == "pending"
            )
            assert retained.result == {"chunks": [{"content": "retained parse"}]}
            assert retained.result_url is None
            with pytest.raises(PermissionDeniedException):
                await get_job_result_for_user(
                    db, job_id=jobId, user_id="another-user", shouldSignResultURL=False
                )
    finally:
        await asyncEngine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("isTimeout", [False, True])
async def test_signed_job_read_preserves_credential_failure_classification(
    postgresqlPrimaryRuntime, monkeypatch: pytest.MonkeyPatch, isTimeout: bool
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.services.jobs.read_service import get_job_result_for_user
    from app.core.exception_handlers import setup_exception_handlers
    from fastapi import FastAPI
    import httpx
    from shared.core.exceptions.domain_exceptions import (
        UnavailableException,
        TimeoutException,
    )
    from shared.models.database.job import Job
    from shared.services.storage.job_file_storage import JobFileStorage

    engine, jobId, _ = postgresqlPrimaryRuntime
    with Session(engine) as db:
        userId: str = _require(db.get(Job, jobId)).user_id
    failure: Exception = (
        TimeoutError("Credential request exceeded original deadline")
        if isTimeout
        else UnavailableException("Credential provider unavailable", retry_after=1)
    )

    async def refuseSigning(*arguments: object, **options: object) -> None:
        raise failure

    monkeypatch.setattr(JobFileStorage, "generateDownloadUrlAsync", refuseSigning)
    asyncEngine = create_async_engine(engine.url.set(drivername="postgresql+asyncpg"))
    try:
        async with AsyncSession(asyncEngine) as db:
            retained = await get_job_result_for_user(
                db, job_id=jobId, user_id=userId, shouldSignResultURL=False
            )
            assert retained.job_result_id and retained.parsing_status == "completed"
            with pytest.raises(TimeoutException if isTimeout else UnavailableException):
                await get_job_result_for_user(db, job_id=jobId, user_id=userId)
        api: FastAPI = FastAPI()
        setup_exception_handlers(api)

        @api.get("/signed-job")
        async def getSignedJob() -> dict[str, object]:
            async with AsyncSession(asyncEngine) as session:
                return (
                    await get_job_result_for_user(session, job_id=jobId, user_id=userId)
                ).model_dump(mode="json")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api, raise_app_exceptions=False),
            base_url="http://contract.invalid",
        ) as client:
            response = await client.get("/signed-job")
        assert response.status_code == (504 if isTimeout else 503)
        assert response.json()["error"]["code"] == (
            "DEADLINE_EXCEEDED" if isTimeout else "UNAVAILABLE"
        )
    finally:
        await asyncEngine.dispose()


@pytest.mark.parametrize("field", ["content", "metadata"])
def test_pg_input_budget_rejects_oversized_raw_revision_before_hydration(
    postgresqlPrimaryRuntime, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    from sqlalchemy import event
    from sqlalchemy.engine import Connection
    from shared.core.config import settings
    from shared.models.database.document import DocumentChunk
    from shared.services.retrieval.lexical.deadline import IndexingDeadline
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.pgpublication import PostgresqlPublication

    engine, jobId, ownerToken = postgresqlPrimaryRuntime
    context = getContext(engine, jobId)
    with Session(engine) as db:
        chunk = db.scalars(
            select(DocumentChunk).where(
                DocumentChunk.job_result_id == context.revisionId
            )
        ).first()
        assert chunk is not None
        if field == "content":
            chunk.content = "raw input body" * 4096
        else:
            chunk.chunk_metadata = {"large": "raw metadata value" * 1024}
        db.commit()
    monkeypatch.setattr(settings, "KNOWHERE_INDEXING_INPUT_MAXIMUM_BYTES", 32 * 1024)
    hydrationQueries: list[str] = []

    def observeHydration(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        executionContext: object,
        executemany: bool,
    ) -> None:
        if statement.startswith(
            ("SELECT document_sections.section_id,", "SELECT document_chunks.id,")
        ):
            hydrationQueries.append(statement)

    event.listen(engine, "before_cursor_execute", observeHydration)
    try:
        adapter = PostgresqlPublication(
            sessionFactory=sessionmaker(engine, expire_on_commit=False),
            deadline=IndexingDeadline(300),
        )
        with pytest.raises(LexicalIndexError, match="(?i)(budget|byte|limit)"):
            adapter._readInput(context, ownerToken)
        assert hydrationQueries == []
    finally:
        event.remove(engine, "before_cursor_execute", observeHydration)


def test_pg_assigned_input_skips_unused_raw_budget_and_hydration(
    postgresqlPrimaryRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import event
    from sqlalchemy.engine import Connection
    from shared.core.config import settings
    from shared.models.database.document import DocumentChunk
    from shared.models.database.job_result import JobResult
    from shared.services.retrieval.lexical.deadline import IndexingDeadline
    from shared.services.retrieval.lexical.pgpublication import PostgresqlPublication

    engine, jobId, ownerToken = postgresqlPrimaryRuntime
    context = getContext(engine, jobId)
    with Session(engine) as db:
        result = _require(db.get(JobResult, context.revisionId))
        result.indexing_input_key = "indexing-input/v1/assigned-retained.zlib"
        result.indexing_input_checksum = "retained-checksum"
        result.indexing_input_compiler = "original-retained-compiler"
        chunk = _require(
            db.scalars(
                select(DocumentChunk).where(
                    DocumentChunk.job_result_id == context.revisionId
                )
            ).first()
        )
        chunk.content = "unused old source" * 1024
        db.commit()
    monkeypatch.setattr(settings, "KNOWHERE_INDEXING_INPUT_MAXIMUM_BYTES", 1024)
    queries: list[str] = []

    def observeRawRead(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        executionContext: object,
        executemany: bool,
    ) -> None:
        if "FROM document_sections" in statement or "FROM document_chunks" in statement:
            queries.append(statement)

    event.listen(engine, "before_cursor_execute", observeRawRead)
    try:
        adapter = PostgresqlPublication(
            sessionFactory=sessionmaker(engine, expire_on_commit=False),
            deadline=IndexingDeadline(300),
        )
        state = adapter._readInput(context, ownerToken)
        assert state.key == "indexing-input/v1/assigned-retained.zlib"
        assert (
            state.compiler == "original-retained-compiler"
            and state.sections == ()
            and state.chunks == ()
        )
        assert queries == []
    finally:
        event.remove(engine, "before_cursor_execute", observeRawRead)


@pytest.mark.parametrize("hasOversizedMetadata", [False, True])
def test_pg_assigned_input_bounds_graph_metadata_before_hydration(
    postgresqlPrimaryRuntime,
    monkeypatch: pytest.MonkeyPatch,
    hasOversizedMetadata: bool,
) -> None:
    from sqlalchemy import event
    from sqlalchemy.engine import Connection
    from shared.core.config import settings
    from shared.models.database.document import Document, DocumentChunk, DocumentSection
    from shared.models.database.job_result import JobResult
    from shared.services.retrieval.lexical.deadline import IndexingDeadline
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.input import LexicalInput
    from shared.services.retrieval.lexical.pgpublication import PostgresqlPublication
    from shared.services.retrieval.lexical.storage import LexicalInputStorage
    from shared.services.retrieval.lexical.unit import LexicalUnit

    engine, jobId, ownerToken = postgresqlPrimaryRuntime
    context = getContext(engine, jobId)
    with Session(engine) as db:
        sectionId = _require(
            db.scalars(
                select(DocumentSection.section_id).where(
                    DocumentSection.job_result_id == context.revisionId
                )
            ).first()
        )
    payload = LexicalInput(
        compilerFingerprint="original-assigned-compiler",
        units=(
            LexicalUnit(
                documentId=context.documentId,
                revisionId=context.revisionId,
                unitId="assigned-unit",
                sectionId=sectionId,
                contentTokens="frozen tokens",
            ),
        ),
    )
    key, checksum, size = LexicalInputStorage().saveWithSize(
        jobId=jobId, revisionId=context.revisionId, payload=payload
    )
    with Session(engine) as db:
        result = _require(db.get(JobResult, context.revisionId))
        result.indexing_input_key, result.indexing_input_checksum = key, checksum
        result.indexing_input_compiler, result.indexing_input_bytes = (
            payload.compilerFingerprint,
            size,
        )
        chunk = _require(
            db.scalars(
                select(DocumentChunk).where(
                    DocumentChunk.job_result_id == context.revisionId
                )
            ).first()
        )
        chunk.content = "unused raw source" * 65536
        chunk.chunk_metadata = (
            {"empty_values": [""] * 6000} if hasOversizedMetadata else {}
        )
        db.commit()
    monkeypatch.setattr(settings, "KNOWHERE_INDEXING_INPUT_MAXIMUM_BYTES", 32 * 1024)
    graphQueries: list[str] = []

    def rejectPrematureHydration(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        executionContext: object,
        executemany: bool,
    ) -> None:
        if statement.startswith("SELECT document_chunks.id,"):
            raise AssertionError("Assigned input allocated unused raw content")
        if statement.startswith(
            "SELECT document_chunks.chunk_type, document_chunks.chunk_metadata"
        ):
            graphQueries.append(statement)
            if hasOversizedMetadata:
                raise AssertionError(
                    "Oversized graph metadata reached hydration before its publication budget"
                )

    event.listen(engine, "before_cursor_execute", rejectPrematureHydration)
    try:
        adapter = PostgresqlPublication(
            sessionFactory=sessionmaker(engine, expire_on_commit=False),
            deadline=IndexingDeadline(300),
        )
        if hasOversizedMetadata:
            with pytest.raises(LexicalIndexError, match="hydration budget"):
                adapter.run(context, ownerToken=ownerToken)
            assert graphQueries == []
        else:
            adapter.run(context, ownerToken=ownerToken)
            assert len(graphQueries) == 1
    finally:
        event.remove(engine, "before_cursor_execute", rejectPrematureHydration)
    with Session(engine) as db:
        document = _require(db.get(Document, context.documentId))
        assert document.current_job_result_id == (
            None if hasOversizedMetadata else context.revisionId
        )
