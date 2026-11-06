"""Reject oversized raw input before ORM allocation, compilation or native writes."""

from __future__ import annotations

from typing import cast


import pytest
from sqlalchemy import event, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tests.contract.test_indexing_execution_contract import parsedRuntime as parsedRuntime  # noqa: F401


def _admitNative(worker: object, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.scope import LexicalScope

    def admit(*, scope: LexicalScope, native: object) -> tuple[str, int]:
        with Session(engine) as db:
            control = db.get(LexicalBackendControl, 1)
            assert control is not None
            db.add(LexicalNamespaceIndex(id=scope.fingerprint, ownerId=scope.ownerId,
                namespace=scope.namespace, corpusKind=scope.corpusKind, indexName="budget-native",
                indexUuid="budget-uuid", state="open"))
            db.commit()
            return "budget-uuid", control.epoch
    monkeypatch.setattr(getattr(worker, "coordination"), "ensureIndex", admit)


@pytest.mark.parametrize("shape", ["content", "metadata", "legacy_search", "empty_rows"])
def test_native_raw_input_budget_rejects_before_any_canonical_orm_load(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch, shape: str,
) -> None:
    from shared.core.config import settings
    from shared.models.database.document import Document, DocumentChunk, DocumentSection
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.services.retrieval.lexical.worker import IndexingWorker

    engine, jobId = parsedRuntime
    with Session(engine) as db:
        chunk = db.scalars(select(DocumentChunk)).one()
        if shape == "content":
            chunk.content = "A" * 40_000
        elif shape == "metadata":
            chunk.chunk_metadata = {"empty_values": [""] * 4_000}
        elif shape == "legacy_search":
            chunk.term_search_text = "legacy" * 16_384
        else:
            db.add_all([DocumentChunk(id=f"empty-{number}", chunk_id=f"empty-{number}",
                user_id=chunk.user_id, namespace=chunk.namespace, document_id=chunk.document_id,
                job_result_id=chunk.job_result_id, section_id=chunk.section_id, chunk_type="text", content="",
                sort_order=number) for number in range(40)])
        db.commit()
    monkeypatch.setattr(settings, "KNOWHERE_INDEXING_INPUT_MAXIMUM_BYTES", 32_768)
    hydrated: list[str] = []
    reached: list[str] = []
    def rejectHydration(target: object, context: object) -> None:
        hydrated.append(type(target).__name__)
        raise AssertionError("Raw canonical ORM hydration preceded the input byte budget")
    def rejectLaterStage(*arguments: object, **options: object) -> None:
        reached.append("compile-or-native-write")
        raise AssertionError("Oversized input reached compilation/native writing")
    worker: IndexingWorker = IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False))
    _admitNative(worker, engine, monkeypatch)
    monkeypatch.setattr(worker, "_compileInput", rejectLaterStage)
    monkeypatch.setattr(worker, "_writeBuild", rejectLaterStage)
    event.listen(DocumentSection, "load", rejectHydration)
    event.listen(DocumentChunk, "load", rejectHydration)
    try:
        assert worker.run(jobId) == "failed"
    finally:
        event.remove(DocumentSection, "load", rejectHydration)
        event.remove(DocumentChunk, "load", rejectHydration)
    assert hydrated == []
    assert reached == []
    with Session(engine) as db:
        job = db.get(Job, jobId)
        assert job is not None
        assert (job.status, job.parsing_status, job.indexing_status, job.failure_stage) == ("failed", "completed", "failed", "indexing")
        assert (job.billing_status, job.credits_charged) == ("charged", 123456)
        assert db.scalars(select(JobResult).where(JobResult.job_id == jobId)).one().result_s3_key == "results/execution.zip"
        assert db.scalars(select(Document)).one().current_job_result_id is None
        attempt = db.scalars(select(IndexingAttempt)).one()
        assert attempt.executionCount == 1 and attempt.state == "failed"
        assert "hydration budget" in str(attempt.errorMessage)


def test_assigned_native_source_reads_only_bounded_section_id_projection(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core.config import settings
    from shared.models.database.document import DocumentChunk, DocumentSection
    from shared.models.database.job_result import JobResult
    from shared.services.retrieval.lexical.worker import IndexingWorker, _Context
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex

    engine, jobId = parsedRuntime
    with Session(engine) as db:
        result = db.scalars(select(JobResult)).one()
        result.indexing_input_key = "retained-assigned-input"
        result.indexing_input_checksum = "a" * 64
        sectionIds: set[str] = set(db.scalars(select(DocumentSection.section_id)))
        db.execute(update(DocumentSection).values(summary="unused" * 1_000))
        db.execute(update(DocumentChunk).values(content="unused" * 1_000_000, term_search_text="unused" * 1_000_000, chunk_metadata={"unused": "x" * 1_000}))
        db.commit()
    monkeypatch.setattr(settings, "KNOWHERE_INDEXING_INPUT_MAXIMUM_BYTES", 32_768)
    worker: IndexingWorker = IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False))
    _admitNative(worker, engine, monkeypatch)
    context = worker._readContext(jobId)
    assert isinstance(context, _Context)
    ownerToken = worker._startExecution(context)
    assert ownerToken is not None
    identity, epoch = worker.coordination.ensureIndex(scope=context.scope, native=cast(OpenSearchLexicalIndex, None))
    claim = worker._claim(context, ownerToken=ownerToken, indexUuid=identity, epoch=epoch)
    assert claim is not None
    hydrated: list[str] = []
    def rejectHydration(target: object, context: object) -> None:
        hydrated.append(type(target).__name__)
        raise AssertionError("Assigned input loaded unused canonical payloads")
    event.listen(DocumentSection, "load", rejectHydration)
    event.listen(DocumentChunk, "load", rejectHydration)
    try:
        source = worker._readInputSource(claim)
    finally:
        event.remove(DocumentSection, "load", rejectHydration)
        event.remove(DocumentChunk, "load", rejectHydration)
    assert hydrated == []
    assert source.key == "retained-assigned-input" and source.chunks == ()
    assert {row.section_id for row in source.sections} == sectionIds
    assert all(row.summary == row.section_path == row.section_title == "" for row in source.sections)


def test_serving_manifest_projects_needed_metadata_without_hydrating_raw_bodies(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core.config import settings
    from shared.models.database.document import DocumentChunk, DocumentSection
    from shared.models.database.job_result import JobResult
    from shared.services.retrieval.publication_models import DocumentPublicationScope
    from shared.services.retrieval.serving_manifest import build_revision_serving_payload

    engine, _ = parsedRuntime
    with Session(engine) as db:
        result = db.scalars(select(JobResult)).one()
        scope = DocumentPublicationScope(user_id="execution-user", namespace="execution-contract",
            document_id="doc_execution", job_result_id=result.id, source_file_name="report.pdf")
        expected = build_revision_serving_payload(db, scope=scope)
        db.execute(update(DocumentChunk).values(content="unused" * 1_000_000,
            term_search_text="unused" * 1_000_000, chunk_metadata={"unused": "x" * 100_000}))
        db.commit()
    monkeypatch.setattr(settings, "KNOWHERE_INDEXING_INPUT_MAXIMUM_BYTES", 32_768)
    hydrated: list[str] = []
    def rejectHydration(target: object, context: object) -> None:
        hydrated.append(type(target).__name__)
        raise AssertionError("Serving metadata loaded raw canonical ORM payloads")
    event.listen(DocumentSection, "load", rejectHydration)
    event.listen(DocumentChunk, "load", rejectHydration)
    try:
        with Session(engine) as db:
            assert build_revision_serving_payload(db, scope=scope) == expected
    finally:
        event.remove(DocumentSection, "load", rejectHydration)
        event.remove(DocumentChunk, "load", rejectHydration)
    assert hydrated == []


def test_input_budget_is_scoped_and_allows_same_rows_with_a_larger_byte_budget(
    parsedRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core.config import settings
    from shared.models.database.document import DocumentChunk
    from shared.models.database.job_result import JobResult
    from shared.services.retrieval.corpus_storage import CorpusStorage
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.input_budget import LexicalInputBudget

    engine, _ = parsedRuntime
    with Session(engine) as db:
        chunk = db.scalars(select(DocumentChunk)).one()
        revision: str = db.scalars(select(JobResult.id)).one()
        db.add_all([DocumentChunk(id=f"framed-{number}", chunk_id=f"framed-{number}",
            user_id=chunk.user_id, namespace=chunk.namespace, document_id=chunk.document_id,
            job_result_id=chunk.job_result_id, section_id=chunk.section_id, chunk_type="text", content="",
            sort_order=number) for number in range(40)])
        db.commit()
        private: CorpusStorage = CorpusStorage("PRIVATE")
        monkeypatch.setattr(settings, "KNOWHERE_INDEXING_INPUT_MAXIMUM_BYTES", 32_768)
        with pytest.raises(LexicalIndexError, match="estimated_working_bytes"):
            LexicalInputBudget.assertCanHydrate(db, storage=private, documentId="doc_execution", revisionId=revision)
        assert LexicalInputBudget.assertCanHydrate(db, storage=private, documentId="different-document", revisionId=revision) == 0
        assert LexicalInputBudget.assertCanHydrate(db, storage=private, documentId="doc_execution", revisionId="different-revision") == 0
        assert LexicalInputBudget.assertCanHydrate(db, storage=CorpusStorage("DEMO"), documentId="doc_execution", revisionId=revision) == 0
        monkeypatch.setattr(settings, "KNOWHERE_INDEXING_INPUT_MAXIMUM_BYTES", 1_048_576)
        assert LexicalInputBudget.assertCanHydrate(db, storage=private, documentId="doc_execution", revisionId=revision) > 32_768 * 4
