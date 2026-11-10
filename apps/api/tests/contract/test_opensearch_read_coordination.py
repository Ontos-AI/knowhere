"""Verify read-only publication capture and asynchronous native recency hints."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    clear_application_modules,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)

if TYPE_CHECKING:
    from shared.services.retrieval.lexical.reader import PublishedLexicalRead


@pytest.fixture
def readRuntime(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> Iterator[tuple[Engine, PublishedLexicalRead]]:
    configure_contract_environment(monkeypatch, postgresql_proc)
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    clear_application_modules()
    asyncio.run(prepare_contract_storage())

    from shared.core.config import settings
    from shared.models.database.document import Document
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.models.database.user import User
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.reader import PublishedLexicalRead
    from shared.services.retrieval.serving_generation import lock_namespace_generation

    engine: Engine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))
    reader: PublishedLexicalRead = PublishedLexicalRead(userId="read-owner", namespace="read-budget",
        sessionFactory=sessionmaker(engine, expire_on_commit=False))
    try:
        with Session(engine) as database, database.begin():
            database.add(User(id="read-owner", name="Read owner", email="read-owner@example.invalid"))
            LexicalCoordination.lockControl(database)
            generation = lock_namespace_generation(database, user_id="read-owner", namespace="read-budget")
            generation.generation = 1
            database.add(LexicalNamespaceIndex(id=reader.scope.fingerprint, ownerId="read-owner",
                namespace="read-budget", corpusKind="private", indexName=f"{settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX}-{reader.scope.fingerprint[:48]}",
                indexUuid="read-budget-uuid", state="open"))
            database.flush()
            for position in range(24):
                identity: str = str(position)
                document: Document = Document(document_id=f"doc_read_{identity}", user_id="read-owner", namespace="read-budget")
                database.add_all([document, Job(job_id=f"job_read_{identity}", user_id="read-owner",
                    job_type="document_ingestion", source_type="file", status="done")])
                database.flush()
                database.add(JobResult(id=f"revision_read_{identity}", job_id=f"job_read_{identity}",
                    document_id=document.document_id, delivery_mode="url"))
                database.flush()
                database.add(IndexingAttempt(id=f"attempt_read_{identity}", jobId=f"job_read_{identity}",
                    revisionId=f"revision_read_{identity}", state="completed"))
                database.flush()
                database.add(LexicalBuild(id=f"build_read_{identity}", attemptId=f"attempt_read_{identity}",
                    namespaceIndexId=reader.scope.fingerprint, documentId=document.document_id,
                    revisionId=f"revision_read_{identity}", ownerToken="fixture", state="published",
                    activationGeneration=1))
                document.current_job_result_id = f"revision_read_{identity}"
                document.current_indexing_build_id = f"build_read_{identity}"
        yield engine, reader
    finally:
        reader.close()
        engine.dispose()


def test_capture_reads_all_published_builds_in_one_statement(
    readRuntime: tuple[Engine, PublishedLexicalRead],
) -> None:
    engine, reader = readRuntime
    statements: list[str] = []

    def captureStatement(connection: object, cursor: object, statement: str,
                         parameters: object, context: object, isMany: bool) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", captureStatement)
    try:
        reader.start()
    finally:
        event.remove(engine, "before_cursor_execute", captureStatement)
    assert dict(reader.builds) == {f"doc_read_{position}": f"build_read_{position}" for position in range(24)}
    assert dict(reader.pins) == {f"doc_read_{position}": f"revision_read_{position}" for position in range(24)}
    assert sum("document_indexing_builds" in statement for statement in statements) == 1
    assert len(statements) <= 6


def test_capture_does_not_lock_or_write_coordination_rows(
    readRuntime: tuple[Engine, PublishedLexicalRead],
) -> None:
    engine, reader = readRuntime
    statements: list[str] = []

    def captureStatement(connection: object, cursor: object, statement: str,
                         parameters: object, context: object, isMany: bool) -> None:
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", captureStatement)
    try:
        reader.start()
        reader.assertAlive()
        reader.finish()
        reader.close()
    finally:
        event.remove(engine, "before_cursor_execute", captureStatement)
    assert statements
    assert not [statement for statement in statements if any(fragment in statement for fragment in (
        "for update", "for share", "pg_advisory", "insert into", "update ", "delete from"))]
    assert len(statements) == 1


def test_capture_does_not_wait_for_locked_shared_rows(
    readRuntime: tuple[Engine, PublishedLexicalRead],
) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.serving_generation import lock_namespace_generation

    engine, reader = readRuntime
    completed: bool = False
    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(engine) as database, database.begin():
            LexicalCoordination.lockControl(database, isExclusive=True)
            lock_namespace_generation(database, user_id="read-owner", namespace="read-budget")
            database.scalars(select(LexicalNamespaceIndex).where(
                LexicalNamespaceIndex.id == reader.scope.fingerprint).with_for_update()).one()
            captured = executor.submit(reader.start)
            try:
                captured.result(timeout=1)
                completed = True
            except TimeoutError:
                pass
        captured.result(timeout=5)
    assert completed, "Ordinary retrieval waited for a shared coordination row"
    assert len(reader.builds) == 24


def test_liveness_validation_does_not_queue_behind_unrelated_publication(
    readRuntime: tuple[Engine, PublishedLexicalRead],
) -> None:
    from shared.services.retrieval.serving_generation import lock_namespace_generation

    engine, reader = readRuntime
    reader.start()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(engine) as database, database.begin():
            lock_namespace_generation(database, user_id="read-owner", namespace="read-budget")
            validation = executor.submit(reader.assertAlive)
            completed: bool = validation.done()
            try:
                validation.result(timeout=2)
                completed = True
            except TimeoutError:
                pass
        validation.result(timeout=5)
    assert completed, "An existing reader queued behind an unrelated publication transaction"


@pytest.mark.parametrize("invalidState", ["missing", "wrong-document", "wrong-revision", "wrong-namespace", "retired"])
def test_capture_rejects_incomplete_or_mismatched_published_builds(
    readRuntime: tuple[Engine, PublishedLexicalRead], invalidState: str,
) -> None:
    from shared.models.database.document import Document
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.error import LexicalIndexError

    engine, reader = readRuntime
    with Session(engine) as database, database.begin():
        document: Document = database.scalars(select(Document).where(Document.document_id == "doc_read_0")).one()
        build: LexicalBuild = database.scalars(select(LexicalBuild).where(LexicalBuild.id == "build_read_0")).one()
        if invalidState == "missing":
            document.current_indexing_build_id = "missing-build"
        elif invalidState == "wrong-document":
            build.documentId = "doc_read_1"
        elif invalidState == "wrong-revision":
            build.revisionId = "revision_read_1"
        elif invalidState == "wrong-namespace":
            database.add(LexicalNamespaceIndex(id="other-scope", ownerId="read-owner", namespace="other",
                corpusKind="private", indexName="other-index", state="closed"))
            database.flush()
            build.namespaceIndexId = "other-scope"
        else:
            build.state = "retired"
    with pytest.raises(LexicalIndexError, match="no matching native build"):
        reader.start()


def test_empty_read_preserves_generation_without_initializing_rows(
    readRuntime: tuple[Engine, PublishedLexicalRead],
) -> None:
    from shared.models.database.document import RetrievalNamespaceGeneration
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.lexical.reader import PublishedLexicalRead

    engine, _ = readRuntime
    with Session(engine) as database, database.begin():
        database.add(RetrievalNamespaceGeneration(id="empty-generation", user_id="read-owner",
            namespace="empty-first-read", generation=7))
        control: LexicalBackendControl = database.scalars(select(LexicalBackendControl)).one()
        database.delete(control)
    readers: tuple[PublishedLexicalRead, ...] = tuple(PublishedLexicalRead(userId="read-owner",
        namespace="empty-first-read", sessionFactory=sessionmaker(engine, expire_on_commit=False)) for _ in range(2))
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda reader: reader.start(), readers))
        assert all(not reader.pins and reader.pins.generation == 7 and reader.epoch == 0 for reader in readers)
        with Session(engine) as database:
            assert not list(database.scalars(select(LexicalBackendControl)))
    finally:
        for reader in readers:
            reader.close()


def test_capture_rejects_recovery_mode_without_waiting_for_a_write_lock(
    readRuntime: tuple[Engine, PublishedLexicalRead],
) -> None:
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.services.retrieval.lexical.error import LexicalIndexError

    engine, reader = readRuntime
    with Session(engine) as database, database.begin():
        control: LexicalBackendControl = database.scalars(select(LexicalBackendControl)).one()
        control.mode = "recovering"
    with pytest.raises(LexicalIndexError, match="transition"):
        reader.start()


def test_native_recency_is_coalesced_and_flushed_without_reader_writes(
    readRuntime: tuple[Engine, PublishedLexicalRead],
) -> None:
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.retrieval.lexical.recency import LexicalRecency

    engine, reader = readRuntime
    with Session(engine) as database:
        before = database.scalars(select(LexicalNamespaceIndex.lastUsedAt)).one()
    for _ in range(50):
        LexicalRecency.recordAccess(reader.scope.fingerprint)
    with Session(engine) as database:
        assert database.scalars(select(LexicalNamespaceIndex.lastUsedAt)).one() == before
    statements: list[str] = []

    def captureStatement(connection: object, cursor: object, statement: str,
                         parameters: object, context: object, isMany: bool) -> None:
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", captureStatement)
    try:
        with Session(engine) as database, database.begin():
            LexicalRecency.flushAccess(database)
    finally:
        event.remove(engine, "before_cursor_execute", captureStatement)
    assert len(statements) == 1 and statements[0].startswith("update ")
    with Session(engine) as database:
        assert database.scalars(select(LexicalNamespaceIndex.lastUsedAt)).one() > before
