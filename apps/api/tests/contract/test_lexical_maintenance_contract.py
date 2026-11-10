"""Owned PostgreSQL checks for non-gating copy and input retention."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from uuid import uuid4
from typing import TypeVar
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from shared.testing.contract_runtime import (
    PostgreSQLProcess, clear_application_modules, configure_contract_environment,
    get_contract_database_url, prepare_contract_storage,
)


_Model = TypeVar("_Model")


def requireRow(db: Session, model: type[_Model], identity: str | int) -> _Model:
    row: _Model | None = db.get(model, identity)
    assert row is not None
    return row


@pytest.fixture
def publishedInput(monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess) -> Iterator[tuple[Engine, str, str, str]]:
    configure_contract_environment(monkeypatch, postgresql_proc)
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    clear_application_modules()
    asyncio.run(prepare_contract_storage())
    from shared.core.celery_app import celery_app
    celery_app.tasks.pop("shared.services.retrieval.lexical.maintenance.maintainLexical", None)
    from shared.models.database.user import User
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.document import Document, DocumentMapUnit, DocumentMapUnitIndex
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
    from shared.services.retrieval.lexical.input import LexicalInput
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.storage import LexicalInputStorage
    from shared.services.retrieval.lexical.unit import LexicalUnit
    from shared.services.retrieval.serving_generation import lock_namespace_generation

    engine: Engine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))
    jobId: str = f"job-{uuid4().hex[:20]}"
    documentId: str = f"doc-{uuid4().hex[:20]}"
    namespace: str = f"maintenance-{uuid4().hex}"
    buildId: str = str(uuid4())
    try:
        with Session(engine) as db:
            db.add(User(id="maintenance-user", name="Maintenance", email="maintenance@example.invalid"))
            db.flush()
            db.add(Job(job_id=jobId, user_id="maintenance-user", job_type="document_ingestion", source_type="file", status="running", job_metadata={"document_id": documentId, "namespace": namespace, "source_file_name": "report.pdf"}))
            db.commit()
            finalized = SyncJobSuccessFinalizer().finalize(db, job_id=jobId, result_s3_key="results/maintenance.zip", checksum="checksum", zip_size=100, chunks=[{"chunk_id": "chunk", "type": "text", "content": "retained source", "path": "report.pdf/A", "metadata": {}}], stored_count=1, delivery_mode="url", section_summaries=None)
            assert finalized.response.should_commit()
            db.commit()
            result: JobResult = db.scalars(select(JobResult).where(JobResult.job_id == jobId)).one()
            attempt: IndexingAttempt = db.scalars(select(IndexingAttempt).where(IndexingAttempt.jobId == jobId)).one()
            scope: LexicalScope = LexicalScope(ownerId="maintenance-user", namespace=namespace)
            registry: LexicalNamespaceIndex = LexicalNamespaceIndex(id=scope.fingerprint, ownerId=scope.ownerId, namespace=namespace, corpusKind="private", indexName=f"knowhere-test-{scope.fingerprint[:48]}", indexUuid="test-index", state="open")
            db.add(registry)
            db.flush()
            db.add(LexicalBuild(id=buildId, attemptId=attempt.id, namespaceIndexId=scope.fingerprint, documentId=documentId, revisionId=result.id, ownerToken=str(uuid4()), state="published", legacyCopyState="pending", cleanupState="not_started"))
            db.flush()
            document: Document = db.scalars(select(Document).where(Document.document_id == documentId)).one()
            document.current_job_result_id = result.id
            document.current_indexing_build_id = buildId
            attempt.currentBuildId = buildId
            attempt.state = "succeeded"
            job: Job = db.scalars(select(Job).where(Job.job_id == jobId)).one()
            job.status = "done"
            job.indexing_status = "completed"
            payload: LexicalInput = LexicalInput(compilerFingerprint="test", units=(LexicalUnit(documentId=documentId, revisionId=result.id, unitId="unit", sectionId="section", pathTokens="path", contentTokens="one two"),))
            key, checksum = LexicalInputStorage().save(jobId=jobId, revisionId=result.id, payload=payload)
            result.indexing_input_key = key
            result.indexing_input_checksum = checksum
            db.add(DocumentMapUnit(id="map-unit", document_id=documentId, job_result_id=result.id, unit_id="unit", section_id="section", unit_kind="leaf", path_token_count=1, content_token_count=2))
            db.add(DocumentMapUnitIndex(id="map-index", document_id=documentId, job_result_id=result.id, format_version=1, unit_count=1, token_count=0))
            lock_namespace_generation(db, user_id="maintenance-user", namespace=namespace)
            db.commit()
            revisionId: str = result.id
        yield engine, buildId, namespace, revisionId
    finally:
        engine.dispose()


def test_pg_copy_does_not_hold_namespace_publication_lock(monkeypatch: pytest.MonkeyPatch, publishedInput: tuple[Engine, str, str, str]) -> None:
    from shared.services.retrieval.lexical import legacy
    from shared.services.retrieval.serving_generation import lock_namespace_generation

    engine, buildId, namespace, _ = publishedInput
    from shared.services.retrieval.corpus_storage import CorpusStorage
    from shared.services.retrieval.publication_token_copy import TokenCopyRow
    from shared.models.database.lexicalbuild import LexicalBuild
    copyTokens = legacy.insert_token_records_with_copy
    progressed: list[bool] = []

    def assertPublicationCanProgress(db: Session, rows: list[TokenCopyRow], *, corpus_storage: CorpusStorage) -> None:
        with Session(engine) as concurrent:
            concurrent.execute(text("SET LOCAL lock_timeout='100ms'"))
            lock_namespace_generation(concurrent, user_id="maintenance-user", namespace=namespace)
            concurrent.scalars(select(LexicalBuild).where(LexicalBuild.id == buildId).with_for_update()).one()
            concurrent.commit()
            progressed.append(True)
        copyTokens(db, rows, corpus_storage=corpus_storage)

    monkeypatch.setattr(legacy, "insert_token_records_with_copy", assertPublicationCanProgress)
    assert legacy.LegacyLexicalCopy(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(buildId)
    assert progressed == [True]


def test_pg_copy_repair_remains_available_while_publication_is_gated(publishedInput: tuple[Engine, str, str, str]) -> None:
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.lexical.legacy import LegacyLexicalCopy

    engine, buildId, _, _ = publishedInput
    with Session(engine) as db:
        control: LexicalBackendControl | None = db.get(LexicalBackendControl, 1)
        if control is None:
            control = LexicalBackendControl(id=1, backend="opensearch", mode="rollback", epoch=1)
            db.add(control)
        else:
            control.mode = "rollback"
        db.commit()
    assert LegacyLexicalCopy(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(buildId)


def test_completed_pg_copy_keeps_published_input_until_retirement(
    publishedInput: tuple[Engine, str, str, str],
) -> None:
    from shared.core.config.storage import get_cached_storage_adapter
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.services.retrieval.lexical.input_collector import LexicalInputCollector
    from shared.services.retrieval.lexical.storage import LexicalInputStorage

    engine, buildId, _, revisionId = publishedInput
    with Session(engine) as db:
        result: JobResult = requireRow(db, JobResult, revisionId)
        assert result.indexing_input_key is not None
        key: str = result.indexing_input_key
        build: LexicalBuild = requireRow(db, LexicalBuild, buildId)
        documentId: str = build.documentId
        build.legacyCopyState = "completed"
        # A later admission can change accepted-job state while the old revision serves.
        requireRow(db, Document, documentId).accepted_job_id = None
        # The published result has the canonical binding even if the job hint is stale.
        result.document_id = documentId
        job: Job = requireRow(db, Job, result.job_id)
        job.job_metadata = {**(job.job_metadata or {}), "document_id": "stale-document-hint"}
        db.commit()

    collector = LexicalInputCollector(sessionFactory=sessionmaker(engine, expire_on_commit=False))
    bucket: str = LexicalInputStorage().bucket
    adapter = get_cached_storage_adapter()
    assert not collector.collectAssigned(revisionId)
    with Session(engine) as db:
        result = requireRow(db, JobResult, revisionId)
        assert result.indexing_input_gc_state == "retained"
        assert result.indexing_input_gc_attempts == 0
        requireRow(db, Document, documentId).status = "archived"
        db.commit()
    assert adapter.exists(key, bucket=bucket)

    assert collector.collectAssigned(revisionId)
    with Session(engine) as db:
        result = requireRow(db, JobResult, revisionId)
        assert result.indexing_input_gc_state == "deleted"
        assert result.result_s3_key == "results/maintenance.zip"
    assert not adapter.exists(key, bucket=bucket)


def test_pending_copy_preserves_input(publishedInput: tuple[Engine, str, str, str]) -> None:
    from shared.models.database.document import Document
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.services.retrieval.lexical.input_collector import LexicalInputCollector
    from shared.core.config.storage import get_cached_storage_adapter
    from shared.services.retrieval.lexical.storage import LexicalInputStorage

    engine, buildId, _, revisionId = publishedInput
    with Session(engine) as db:
        build: LexicalBuild = requireRow(db, LexicalBuild, buildId)
        requireRow(db, Document, build.documentId).status = "archived"
        db.commit()
    collector = LexicalInputCollector(sessionFactory=sessionmaker(engine, expire_on_commit=False))
    assert not collector.collectAssigned(revisionId)
    with Session(engine) as db:
        result: JobResult = requireRow(db, JobResult, revisionId)
        assert result.indexing_input_gc_state == "retained"
        assert result.indexing_input_key is not None
        assert get_cached_storage_adapter().exists(result.indexing_input_key, bucket=LexicalInputStorage().bucket)


def test_old_eligible_failed_candidate_keeps_assigned_input(publishedInput: tuple[Engine, str, str, str]) -> None:
    from datetime import datetime
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.indexing import IndexingAttempt
    from shared.services.retrieval.lexical.input_collector import LexicalInputCollector

    engine, buildId, _, revisionId = publishedInput
    with Session(engine) as db:
        result: JobResult = requireRow(db, JobResult, revisionId)
        result.created_at = datetime(1990, 1, 1)
        job: Job = requireRow(db, Job, result.job_id)
        job.status = "failed"
        job.indexing_status = "failed"
        job.failure_stage = "indexing"
        build: LexicalBuild = requireRow(db, LexicalBuild, buildId)
        build.legacyCopyState = "not_started"
        build.state = "failed"
        requireRow(db, Document, build.documentId).current_job_result_id = None
        requireRow(db, IndexingAttempt, str(job.current_indexing_attempt_id)).state = "failed"
        db.commit()
    assert not LexicalInputCollector(sessionFactory=sessionmaker(engine, expire_on_commit=False)).collectAssigned(revisionId)
    with Session(engine) as db:
        assert requireRow(db, JobResult, revisionId).indexing_input_gc_state == "retained"


def test_failed_deletion_is_durable_and_fences_late_assignment(monkeypatch: pytest.MonkeyPatch, publishedInput: tuple[Engine, str, str, str]) -> None:
    from shared.models.database.document import Document
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.input_collector import LexicalInputCollector
    from shared.services.retrieval.lexical.input_retention import LexicalInputRetention
    from shared.core.config.storage import get_cached_storage_adapter

    engine, buildId, _, revisionId = publishedInput
    with Session(engine) as db:
        build: LexicalBuild = requireRow(db, LexicalBuild, buildId)
        build.legacyCopyState = "completed"
        requireRow(db, Document, build.documentId).status = "archived"
        db.commit()
    adapter = get_cached_storage_adapter()
    deleteObject = adapter.delete_object
    monkeypatch.setattr(adapter, "delete_object", lambda *arguments, **keywords: False)
    collector = LexicalInputCollector(sessionFactory=sessionmaker(engine, expire_on_commit=False))
    assert not collector.collectAssigned(revisionId)
    with Session(engine) as db:
        result: JobResult = db.scalars(select(JobResult).where(JobResult.id == revisionId).with_for_update(of=JobResult)).one()
        assert result.indexing_input_gc_state == "deleting"
        assert result.indexing_input_gc_error == "LexicalIndexError"
        assert result.indexing_input_gc_attempts == 1
        with pytest.raises(LexicalIndexError):
            LexicalInputRetention.assertRetained(db, result, key=result.indexing_input_key)
    monkeypatch.setattr(adapter, "delete_object", deleteObject)
    assert collector.collectAssigned(revisionId)
    with Session(engine) as db:
        assert requireRow(db, JobResult, revisionId).indexing_input_gc_state == "deleted"
        assert requireRow(db, JobResult, revisionId).indexing_input_gc_attempts == 2


def test_orphan_inventory_keeps_winner_and_live_preparation_then_reclaims(monkeypatch: pytest.MonkeyPatch, publishedInput: tuple[Engine, str, str, str]) -> None:
    from datetime import timedelta
    from io import BytesIO
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.lexicalinputinventory import LexicalInputInventory
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.services.retrieval.lexical.input_collector import LexicalInputCollector
    from shared.services.retrieval.lexical.input_retention import LexicalInputRetention
    from shared.services.retrieval.lexical.storage import LexicalInputStorage
    from shared.core.config.storage import get_cached_storage_adapter

    engine, buildId, _, revisionId = publishedInput
    with Session(engine) as db:
        result: JobResult = requireRow(db, JobResult, revisionId)
        assert result.indexing_input_key is not None
        winner: str = result.indexing_input_key
        jobId: str = result.job_id
        requireRow(db, LexicalBuild, buildId).legacyCopyState = "completed"
        job: Job = requireRow(db, Job, jobId)
        assert job.current_indexing_attempt_id is not None
        attemptId: str = job.current_indexing_attempt_id
        requireRow(db, IndexingAttempt, attemptId).state = "running"
        db.commit()
    orphan: str = f"indexing-input/v1/{jobId}/{revisionId}/{'a' * 64}-{uuid4().hex}.json.zlib"
    adapter = get_cached_storage_adapter()
    bucket: str = LexicalInputStorage().bucket
    adapter.upload_fileobj(BytesIO(b"orphan"), orphan, bucket=bucket)
    collector = LexicalInputCollector(sessionFactory=sessionmaker(engine, expire_on_commit=False))
    assert collector.discover() == 2
    assert not collector.collectOrphan(winner)
    assert not collector.collectOrphan(orphan)
    identity: str = LexicalInputRetention.getIdentity(bucket=bucket, key=orphan)
    with Session(engine) as db:
        requireRow(db, LexicalInputInventory, identity).firstSeenAt -= timedelta(days=1)
        db.commit()
    assert not collector.collectOrphan(orphan)  # Even expired but unsettled work retains it.
    with Session(engine) as db:
        requireRow(db, IndexingAttempt, attemptId).state = "succeeded"
        db.commit()
    assert collector.collectOrphan(orphan)
    assert not adapter.exists(orphan, bucket=bucket)
    assert adapter.exists(winner, bucket=bucket)


@pytest.mark.parametrize("controlState", [
    None, ("postgresql", "serving"), ("postgresql", "gated"), ("opensearch", "gated"),
])
def test_worker_maintenance_waits_for_persisted_opensearch_serving(
    monkeypatch: pytest.MonkeyPatch,
    publishedInput: tuple[Engine, str, str, str],
    controlState: tuple[str, str] | None,
) -> None:
    from shared.core.config.storage import get_cached_storage_adapter
    from shared.models.database.document import Document
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.lexical import maintenance
    from shared.services.retrieval.lexical.cleanup import LexicalCleanup
    from shared.services.retrieval.lexical.storage import LexicalInputStorage

    engine, buildId, _, revisionId = publishedInput
    with Session(engine) as db:
        build: LexicalBuild = requireRow(db, LexicalBuild, buildId)
        build.legacyCopyState = "completed"
        requireRow(db, Document, build.documentId).status = "archived"
        control: LexicalBackendControl = requireRow(db, LexicalBackendControl, 1)
        if controlState is None:
            db.delete(control)
        else:
            control.backend, control.mode = controlState
        db.commit()
    monkeypatch.setattr(
        maintenance, "get_sync_session_factory", lambda: sessionmaker(engine, expire_on_commit=False),
    )
    monkeypatch.setattr(LexicalCleanup, "closeEmptyNamespaces", lambda self: 0)

    assert maintenance.maintainLexical() == 0
    with Session(engine) as db:
        result: JobResult = requireRow(db, JobResult, revisionId)
        assert result.indexing_input_gc_state == "retained"
        assert result.indexing_input_gc_attempts == 0
        assert result.indexing_input_key is not None
        assert get_cached_storage_adapter().exists(result.indexing_input_key, bucket=LexicalInputStorage().bucket)
        if controlState is None:
            assert db.get(LexicalBackendControl, 1) is None


def test_maintenance_visits_later_pending_builds_when_old_work_stays_blocked(monkeypatch: pytest.MonkeyPatch, publishedInput: tuple[Engine, str, str, str]) -> None:
    from datetime import timedelta
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.services.retrieval.lexical import copy_dispatch, maintenance
    from shared.services.retrieval.lexical.legacy import LegacyLexicalCopy
    from shared.services.retrieval.lexical.coordination import LexicalCoordination

    engine, buildId, _, _ = publishedInput
    with Session(engine) as db:
        original: LexicalBuild = requireRow(db, LexicalBuild, buildId)
        now = LexicalCoordination.getDatabaseTime(db)
        for number in range(21):
            db.add(LexicalBuild(id=f"fair-{number:02}", attemptId=original.attemptId,
                namespaceIndexId=original.namespaceIndexId, documentId=original.documentId,
                revisionId=original.revisionId, ownerToken=str(uuid4()), state="retired",
                legacyCopyState="pending", cleanupState="not_started", createdAt=now + timedelta(seconds=number)))
        original.legacyCopyState = "completed"
        db.commit()
    visited: list[str] = []
    monkeypatch.setattr(maintenance, "get_sync_session_factory", lambda: sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(LegacyLexicalCopy, "run", lambda self, identity: visited.append(identity) or False)
    monkeypatch.setattr(copy_dispatch, "_COPY_POOL", SimpleNamespace(trySubmit=lambda operation: operation() or True))
    maintenance.maintainLexical()
    monkeypatch.setattr(LexicalCoordination, "getDatabaseTime", lambda db: now + timedelta(minutes=10))
    maintenance.maintainLexical()
    assert "fair-20" in visited


def test_copy_failure_does_not_skip_same_build_cleanup(monkeypatch: pytest.MonkeyPatch, publishedInput: tuple[Engine, str, str, str]) -> None:
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.services.retrieval.lexical import copy_dispatch, maintenance
    from shared.services.retrieval.lexical.legacy import LegacyLexicalCopy
    from shared.services.retrieval.lexical.cleanup import LexicalCleanup

    engine, buildId, _, _ = publishedInput
    with Session(engine) as db:
        requireRow(db, LexicalBuild, buildId).cleanupState = "pending"
        db.commit()
    cleaned: list[str] = []
    def failCopy(self: object, identity: str) -> bool:
        raise RuntimeError("copy unavailable")
    monkeypatch.setattr(maintenance, "get_sync_session_factory", lambda: sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(LegacyLexicalCopy, "run", failCopy)
    monkeypatch.setattr(copy_dispatch, "_COPY_POOL", SimpleNamespace(trySubmit=lambda operation: operation() or True))
    monkeypatch.setattr(LexicalCleanup, "collect", lambda self, identity: cleaned.append(identity) or True)
    maintenance.maintainLexical()
    assert cleaned == [buildId]


def test_empty_index_closes_and_reopens_same_identity_without_deletion(publishedInput: tuple[Engine, str, str, str]) -> None:
    from typing import cast
    from shared.models.database.document import Document
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.nativestate import NativeLifecycleSnapshot
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    engine, buildId, namespace, _ = publishedInput
    with Session(engine) as db:
        build: LexicalBuild = requireRow(db, LexicalBuild, buildId)
        document: Document = requireRow(db, Document, build.documentId)
        document.status = "archived"
        build.state = "retired"
        build.cleanupState = "completed"
        build.legacyCopyState = "completed"
        registry: LexicalNamespaceIndex = requireRow(db, LexicalNamespaceIndex, build.namespaceIndexId)
        indexName: str = registry.indexName
        db.commit()
    class EmptyNative:
        def __init__(self) -> None:
            self.indexName: str = indexName
            self.isOpen: bool = True
            self.actions: list[bool] = []
        def getLifecycleSnapshot(self, **arguments: object) -> NativeLifecycleSnapshot:
            return NativeLifecycleSnapshot.model_validate({"cluster_uuid": "cluster", "nodes": {"node": {"ephemeral_id": "incarnation"}},
                "metadata": {"indices": {indexName: {"state": "open" if self.isOpen else "close", "settings": {"index": {"uuid": "test-index"}}}}},
                "routing_table": {"indices": {indexName: {"shards": {"0": [{"primary": True, "state": "STARTED"}]}}}},
                "blocks": {"indices": {} if self.isOpen else {indexName: {"4": {"levels": ["read", "write"]}}}}})
        def forScope(self, scope: LexicalScope) -> EmptyNative:
            return self
        def setIndexOpen(self, *, isOpen: bool, **arguments: object) -> None:
            self.isOpen = isOpen
            self.actions.append(isOpen)
        def close(self) -> None:
            pass
        def createIndex(self, **arguments: object) -> None:
            raise AssertionError("existing empty index must not be recreated")
    native: EmptyNative = EmptyNative()
    scope: LexicalScope = LexicalScope(ownerId="maintenance-user", namespace=namespace)
    coordination: LexicalCoordination = LexicalCoordination(sessionmaker(engine, expire_on_commit=False))
    assert coordination.closeEmpty(scope=scope, native=cast(OpenSearchLexicalIndex, native))
    with Session(engine) as db:
        registry = requireRow(db, LexicalNamespaceIndex, scope.fingerprint)
        assert registry.state == "closed"
        assert registry.indexUuid == "test-index"
    assert coordination.ensureIndex(scope=scope, native=cast(OpenSearchLexicalIndex, native))[0] == "test-index"
    assert native.actions == [False, True]


def test_closed_retired_build_reopens_for_exact_cleanup(monkeypatch: pytest.MonkeyPatch, publishedInput: tuple[Engine, str, str, str]) -> None:
    from shared.models.database.document import Document
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical import cleanup
    from shared.services.retrieval.lexical.coordination import LexicalCoordination

    engine, buildId, _, _ = publishedInput
    with Session(engine) as db:
        build: LexicalBuild = requireRow(db, LexicalBuild, buildId)
        registryId: str = build.namespaceIndexId
        requireRow(db, LexicalNamespaceIndex, registryId).state = "closed"
        document: Document = requireRow(db, Document, build.documentId)
        document.current_indexing_build_id = None
        document.current_job_result_id = None
        document.status = "archived"
        build.state = "retired"
        build.cleanupState = "pending"
        build.legacyCopyState = "completed"
        db.commit()
    actions: list[str] = []
    def reopen(self: LexicalCoordination, **arguments: object) -> tuple[str, int]:
        with Session(engine) as db:
            requireRow(db, LexicalNamespaceIndex, registryId).state = "open"
            db.commit()
        actions.append("open")
        return "test-index", 1
    class Native:
        def __init__(self, **arguments: object) -> None:
            pass
        def getClusterUuid(self) -> None:
            return None
        def getIndexUuid(self) -> str:
            return "test-index"
        def deleteBuild(self, identity: str) -> bool:
            assert identity == buildId
            actions.append("delete_build")
            return True
        def close(self) -> None:
            pass
    monkeypatch.setattr(LexicalCoordination, "ensureIndex", reopen)
    monkeypatch.setattr(cleanup, "OpenSearchLexicalIndex", Native)
    assert cleanup.LexicalCleanup(sessionFactory=sessionmaker(engine, expire_on_commit=False)).collect(buildId)
    assert actions == ["open", "delete_build"]


@pytest.mark.parametrize("retirement", ["archived", "superseded", "canonical_removed"])
def test_ineligible_failed_candidate_reclaims_only_working_input(publishedInput: tuple[Engine, str, str, str], retirement: str) -> None:
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.indexing import IndexingAttempt
    from shared.services.retrieval.lexical.input_collector import LexicalInputCollector

    engine, buildId, _, revisionId = publishedInput
    with Session(engine) as db:
        result: JobResult = requireRow(db, JobResult, revisionId)
        job: Job = requireRow(db, Job, result.job_id)
        build: LexicalBuild = requireRow(db, LexicalBuild, buildId)
        document: Document = requireRow(db, Document, build.documentId)
        job.status = "failed"
        job.indexing_status = "failed"
        job.failure_stage = "indexing"
        build.state = "failed"
        build.legacyCopyState = "not_started"
        requireRow(db, IndexingAttempt, str(job.current_indexing_attempt_id)).state = "failed"
        document.current_job_result_id = None
        if retirement == "archived":
            document.status = "archived"
        elif retirement == "superseded":
            document.accepted_job_id = None
        else:
            result.result_s3_key = None
            result.inline_payload = None
        db.commit()
    assert LexicalInputCollector(sessionFactory=sessionmaker(engine, expire_on_commit=False)).collectAssigned(revisionId)
    with Session(engine) as db:
        result = requireRow(db, JobResult, revisionId)
        assert result.indexing_input_gc_state == "deleted"
        assert result.result_s3_key == (None if retirement == "canonical_removed" else "results/maintenance.zip")


def test_orphan_survives_inventory_failure_and_canonical_result_removal(monkeypatch: pytest.MonkeyPatch, publishedInput: tuple[Engine, str, str, str]) -> None:
    from datetime import timedelta
    from sqlalchemy import delete
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalinputinventory import LexicalInputInventory
    from shared.services.retrieval.lexical.input_collector import LexicalInputCollector
    from shared.services.retrieval.lexical.input_retention import LexicalInputRetention
    from shared.services.retrieval.lexical.storage import LexicalInputStorage
    from shared.core.config.storage import get_cached_storage_adapter

    engine, _, _, revisionId = publishedInput
    with Session(engine) as db:
        result: JobResult = requireRow(db, JobResult, revisionId)
        assert result.indexing_input_key is not None
        key: str = result.indexing_input_key
    collector = LexicalInputCollector(sessionFactory=sessionmaker(engine, expire_on_commit=False))
    adapter = get_cached_storage_adapter()
    listPage = adapter.list_object_page
    def failInventory(**arguments: object) -> None:
        raise OSError("inventory unavailable")
    monkeypatch.setattr(adapter, "list_object_page", failInventory)
    with pytest.raises(OSError):
        collector.discover()
    bucket: str = LexicalInputStorage().bucket
    with Session(engine) as db:
        cursor: LexicalInputInventory = db.scalars(select(LexicalInputInventory).where(LexicalInputInventory.state == "cursor")).one()
        assert cursor.lastError == "OSError"
        assert cursor.scanAfter is None
        cursor.nextScanAt -= timedelta(days=1)
        db.commit()
    monkeypatch.setattr(adapter, "list_object_page", listPage)
    assert collector.discover() == 1
    with Session(engine) as db:
        identity: str = LexicalInputRetention.getIdentity(bucket=bucket, key=key)
        requireRow(db, LexicalInputInventory, identity).firstSeenAt -= timedelta(days=1)
        db.execute(delete(JobResult).where(JobResult.id == revisionId))
        db.commit()
    assert collector.collectOrphan(key)
    assert not adapter.exists(key, bucket=bucket)
