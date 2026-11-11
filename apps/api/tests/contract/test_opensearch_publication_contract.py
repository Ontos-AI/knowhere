"""Prepared parses become searchable only after native visibility and activation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
import json
import os
from datetime import timedelta
from pathlib import Path
import subprocess
import sys
from threading import Event, get_ident
from types import SimpleNamespace
from uuid import uuid4
from typing import Any, Literal, ParamSpec, TypeVar

import httpx
import pytest
from sqlalchemy import create_engine, event, func, select, update
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


@pytest.mark.parametrize("map_tool", ["outline", "node_filter"])
def test_agentic_retrieval_folds_native_maps_without_pg_tokens(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess, map_tool: str,
) -> None:
    endpoint: str = _getNativeEndpoint()
    query: str = "ceruleanevidence"
    document_id: str = "doc_native_contract"
    user_id: str = "native-contract-user"
    expected_chunk: str = "map-evidence"
    chunks: list[dict[str, object]] = [
        {"chunk_id": f"map-noise-{position}", "type": "text", "content": f"Routine maintenance note {position}",
         "path": f"report.pdf/Field Guide/Section {position:02d}", "metadata": {}}
        for position in range(24)
    ]
    chunks.extend([
        {"chunk_id": expected_chunk, "type": "text", "content": f"The test marker is {query}",
         "path": "report.pdf/Field Guide/Final Evidence", "metadata": {}},
        {"chunk_id": "outside-scope", "type": "text", "content": query,
         "path": "report.pdf/Outside Scope", "metadata": {}},
    ])
    engine, job_id, namespace = _prepareParsedJob(monkeypatch, postgresql_proc, preparedChunks=chunks)

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from shared.models.database.document import DocumentChunk, DocumentMapUnitToken, DocumentSection
    from shared.services.retrieval.agent_explore.harness import openai_harness
    from shared.services.retrieval.agent_tools import map_render
    from shared.services.retrieval.app_service import run_retrieval_query
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.stats.recorder import drain_retrieval_hit_stats_updates

    monkeypatch.setenv("AGENT_EXPLORE_HARNESS", "openai")
    monkeypatch.setattr(map_render, "MAP_TOOL_CHAR_BUDGET", 560)
    monkeypatch.setattr(LegacyCopyDispatch, "submit", staticmethod(lambda _build_id: False))
    native = OpenSearchLexicalIndex(endpoint=endpoint, scope=LexicalScope(ownerId=user_id, namespace=namespace),
                                    indexPrefix="knowhere-slice-test")

    class ScriptedModel:
        def __init__(self, target_path: str) -> None:
            parent: str = target_path.rsplit(" / ", 1)[0]
            args: dict[str, object] = {"scope": [{"document_id": document_id, "section_path": parent}]}
            if map_tool == "node_filter":
                args["predicates"] = [{"field": "path", "terms": [parent + "$"], "match": "regex"}]
            self.turns: list[tuple[str, dict[str, object]]] = [(f"corpus_{map_tool}", args)]
            if map_tool == "outline":
                self.turns.append(("corpus_pick", {"pick": ["O1"]}))
            self.turns.extend([
                ("corpus_read", {"refs": [{"document_id": document_id, "section_path": target_path}]}),
                ("corpus_pick", {"pick": ["R1.1"]}),
                ("finish", {}),
            ])
            self.target_path: str = target_path
            self.folded_map: str | None = None

        def chat_completion_raw_with_usage(
            self, *, messages: list[dict[str, str]], **_kwargs: object,
        ) -> tuple[SimpleNamespace, dict[str, int]]:
            name, args = self.turns.pop(0)
            if self.folded_map is None and name != f"corpus_{map_tool}":
                self.folded_map = messages[-1]["content"]
                assert "nodes hidden" in self.folded_map, self.folded_map
                assert self.target_path in self.folded_map, self.folded_map
            call = SimpleNamespace(function=SimpleNamespace(name=name, arguments=json.dumps(args)))
            message = SimpleNamespace(tool_calls=[call], content="")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)]), {"total_tokens": 1}

    async def retrieve() -> dict[str, object]:
        async_engine = create_async_engine(get_contract_database_url())
        try:
            async with AsyncSession(async_engine) as db:
                return await run_retrieval_query(db=db, user_id=user_id, namespace=namespace, query=query, top_k=1,
                                                 use_agentic=True, exclude_document_ids=[], exclude_sections=[])
        finally:
            await drain_retrieval_hit_stats_updates()
            await async_engine.dispose()

    try:
        assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(job_id) == "completed"
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(DocumentMapUnitToken)) == 0
            target_path: str = session.scalars(select(DocumentSection.section_path).join(
                DocumentChunk, DocumentChunk.section_id == DocumentSection.section_id,
            ).where(DocumentChunk.chunk_id == expected_chunk)).one()
        model = ScriptedModel(target_path)
        monkeypatch.setattr(openai_harness, "_resolve_client_and_model", lambda: (model, "scripted"))
        pg_token_reads: list[str] = []

        def record_pg_token_read(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
            if statement.lstrip().upper().startswith("SELECT") and "document_map_unit_tokens" in statement:
                pg_token_reads.append(statement)

        event.listen(Engine, "before_cursor_execute", record_pg_token_read)
        try:
            response = asyncio.run(retrieve())
        finally:
            event.remove(Engine, "before_cursor_execute", record_pg_token_read)
        assert not pg_token_reads
        assert response["router_used"] == "agent_explore"
        assert model.folded_map is not None
        results = response["results"]
        assert isinstance(results, list)
        assert [row["chunk_id"] for row in results] == [expected_chunk]
    finally:
        response = httpx.delete(f"{endpoint}/{native.indexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
        native.close()
        engine.dispose()


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


@pytest.mark.parametrize("outcome", ["success", "failure", "interrupted", "concurrent", "abort", "deferred"])
def test_progressive_scope_replacement_updates_the_published_native_build(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess, outcome: str,
) -> None:
    endpoint = _getNativeEndpoint()
    if outcome == "deferred":
        monkeypatch.setenv("KNOWHERE_OPENSEARCH_PG_COPY_ON_DEMAND", "true")
    other_scope: dict[str, object] = {
        "chunk_id": "other-scope", "type": "text", "content": "cedarcontinuity",
        "path": "report.pdf/Other", "metadata": {"scope_id": "p2-2"},
    }
    engine, job_id, namespace = _prepareParsedJob(
        monkeypatch, postgresql_proc, preparedChunks=[
            {"chunk_id": "baseline-scope", "type": "text", "content": "quartzbaseline",
             "path": "report.pdf/Original", "metadata": {"scope_id": "p1-1"}},
            other_scope,
            {"chunk_id": "third-scope", "type": "text", "content": "maplecontinuity",
             "path": "report.pdf/Third", "metadata": {"scope_id": "p3-3"}},
        ],
    )
    from sqlalchemy.orm import sessionmaker
    from shared.core.config import settings
    from shared.models.database.document import Document, DocumentChunk, DocumentMapUnit, DocumentMapUnitToken
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.lexicalcontrol import LexicalBackendControl
    from shared.models.database.lexicallease import LexicalLease
    from shared.services.retrieval.lexical.cleanup import LexicalCleanup
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.progressivepublication import ProgressivePublication
    from shared.services.retrieval.lexical.storage import LexicalInputStorage
    from shared.services.retrieval.lexical.transition import LexicalTransition, _Binding
    from shared.services.retrieval.lexical.unit import LexicalUnit
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.publication_models import DocumentPublicationScope
    from shared.services.retrieval.publication_scope_content import replace_document_scope_content
    from shared.services.retrieval.scoring.knowhere_hybrid import tokenize_query_for_ranker

    document_id = "doc_native_contract"
    user_id = "native-contract-user"
    native = OpenSearchLexicalIndex(endpoint=endpoint, scope=LexicalScope(ownerId=user_id, namespace=namespace),
        indexPrefix="knowhere-slice-test")
    submitted_copies: list[str] = []

    def record_copy_submission(identity: str) -> bool:
        submitted_copies.append(identity)
        return False

    monkeypatch.setattr(LegacyCopyDispatch, "submit", staticmethod(record_copy_submission))

    def matches(build_id: str, query: str) -> bool:
        return bool(native.search(buildIds=(build_id,),
                                  queryTokens=tuple(tokenize_query_for_ranker(query)), limit=10))

    try:
        worker = IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False))
        assert worker.run(job_id) == "completed"
        with Session(engine) as session:
            document = session.get(Document, document_id)
            assert document is not None and document.current_job_result_id is not None
            assert document.current_indexing_build_id is not None
            baseline_build_id = document.current_indexing_build_id
            revision_id = document.current_job_result_id
            if outcome == "deferred":
                build = session.get(LexicalBuild, baseline_build_id)
                assert build is not None and build.legacyCopyState == "skipped"
                assert not submitted_copies
                assert session.scalar(select(func.count()).select_from(DocumentMapUnitToken)) == 0
        assert matches(baseline_build_id, "quartzbaseline")
        assert matches(baseline_build_id, "cedarcontinuity")
        assert not matches(baseline_build_id, "orchidenrichment")

        scope = DocumentPublicationScope(user_id=user_id, namespace=namespace,
            document_id=document_id, job_result_id=revision_id, source_file_name="report.pdf")
        writer = ProgressivePublication(sessionFactory=sessionmaker(engine, expire_on_commit=False))

        def replace_scope(session: Session, publication_scope: DocumentPublicationScope) -> None:
            replace_document_scope_content(
                session, scope=publication_scope,
                scope_id="p1-1",
                chunks=[{"chunk_id": "refined-scope", "type": "text", "content": "orchidenrichment",
                         "path": "report.pdf/Refined", "metadata": {"scope_id": "p1-1"}}],
            )

        original_write = OpenSearchLexicalIndex.writeBuild
        candidate_ids: list[str] = []
        baseline_text: set[str] = {"quartzbaseline", "cedarcontinuity", "maplecontinuity"}
        staged_text: set[str] = {"orchidenrichment", "cedarcontinuity", "maplecontinuity"}

        def replace_peer_scope(session: Session, publication_scope: DocumentPublicationScope) -> None:
            replace_document_scope_content(session, scope=publication_scope, scope_id="p2-2",
                chunks=[{"chunk_id": "refined-peer", "type": "text", "content": "willowenrichment",
                    "path": "report.pdf/Other", "metadata": {}}])

        class InterruptedWrite(BaseException):
            """Leave durable writer state as a terminated worker would."""

        peer_started = Event()
        peer_writing = Event()
        peer_checked_out = Event()
        peer_thread: int | None = None
        peer: Future[None] | None = None

        def record_checkout(*_: object) -> None:
            if get_ident() == peer_thread:
                peer_checked_out.set()

        def publish_peer() -> None:
            nonlocal peer_thread
            peer_thread = get_ident()
            peer_started.set()
            publisher = ProgressivePublication(sessionFactory=sessionmaker(engine, expire_on_commit=False))
            publisher.publish(scope=scope, update=replace_peer_scope)

        def observe_write(index: OpenSearchLexicalIndex, *, buildId: str, units: tuple[LexicalUnit, ...],
                          beforeBatch: Callable[[], None] | None = None) -> None:
            nonlocal peer
            if candidate_ids:
                peer_writing.set()
            original_write(index, buildId=buildId, units=units, beforeBatch=beforeBatch)
            candidate_ids.append(buildId)
            previous_build_id: str = baseline_build_id
            previous_text: set[str] = baseline_text
            if outcome == "concurrent" and len(candidate_ids) == 2:
                previous_build_id = candidate_ids[0]
                previous_text = staged_text
            with Session(engine) as session:
                current = session.get(Document, document_id)
                assert current is not None and current.current_indexing_build_id == previous_build_id
                assert set(session.scalars(select(DocumentChunk.content))) == previous_text
            assert matches(baseline_build_id, "quartzbaseline")
            if outcome == "failure":
                raise LexicalIndexError("Injected native failure after candidate write")
            if outcome == "interrupted":
                raise InterruptedWrite
            if outcome == "concurrent" and len(candidate_ids) == 1:
                peer = executor.submit(publish_peer)
                assert peer_started.wait(5)
                assert not peer_writing.wait(2), "The second writer reached OpenSearch before the first published"
                assert not peer_checked_out.is_set(), "A queued publication checked out a database connection"

        monkeypatch.setattr(OpenSearchLexicalIndex, "writeBuild", observe_write)
        event.listen(engine.pool, "checkout", record_checkout)
        with ThreadPoolExecutor(max_workers=1) as executor:
            if outcome == "failure":
                with pytest.raises(LexicalIndexError, match="Injected native failure"):
                    writer.publish(scope=scope, update=replace_scope)
            elif outcome == "interrupted":
                with pytest.raises(InterruptedWrite):
                    writer.publish(scope=scope, update=replace_scope)
            else:
                writer.publish(scope=scope, update=replace_scope)
        if outcome == "concurrent":
            assert peer is not None
            peer.result()
        assert len(candidate_ids) == (2 if outcome == "concurrent" else 1)
        if outcome == "interrupted":
            cleanup = LexicalCleanup(sessionFactory=sessionmaker(engine, expire_on_commit=False))
            assert not cleanup.collect(candidate_ids[0])
            assert matches(candidate_ids[0], "orchidenrichment")
            with Session(engine) as session, session.begin():
                session.execute(update(LexicalLease).where(LexicalLease.buildId == candidate_ids[0])
                    .values(expiresAt=func.now() - timedelta(seconds=1)))
            assert not cleanup.collect(candidate_ids[0])
            assert not matches(candidate_ids[0], "orchidenrichment")

        with Session(engine) as session:
            document = session.get(Document, document_id)
            assert document is not None and document.current_job_result_id == revision_id
            content = set(session.scalars(select(DocumentChunk.content).where(
                DocumentChunk.document_id == document_id, DocumentChunk.job_result_id == revision_id,
            )))
            active_build_id = document.current_indexing_build_id
            assert active_build_id is not None
            if outcome in ("failure", "interrupted"):
                assert active_build_id == baseline_build_id
                assert content == baseline_text
                candidate = session.get(LexicalBuild, candidate_ids[0])
                assert candidate is not None and candidate.state == "failed" and candidate.cleanupState == "pending"
            else:
                assert active_build_id == candidate_ids[-1]
                if outcome == "concurrent":
                    staged_text = {"orchidenrichment", "willowenrichment", "maplecontinuity"}
                    first = session.get(LexicalBuild, candidate_ids[0])
                    assert first is not None and first.state == "retired"
                assert content == staged_text
                tokens = set(session.scalars(select(DocumentMapUnitToken.token)))
                if outcome == "deferred":
                    build = session.get(LexicalBuild, active_build_id)
                    assert build is not None and build.legacyCopyState == "skipped"
                    assert not tokens
                else:
                    assert "orchidenrichment" in tokens
                result = session.get(JobResult, revision_id)
                assert result is not None and result.indexing_input_key and result.indexing_input_checksum
                payload = LexicalInputStorage().load(key=result.indexing_input_key, checksum=result.indexing_input_checksum)
                binding = _Binding("private", user_id, namespace, document_id, revision_id, job_id,
                    result.indexing_input_key, result.indexing_input_checksum, result.indexing_input_compiler, active_build_id)
                if outcome == "deferred":
                    assert set(session.execute(select(DocumentMapUnit.unit_id, DocumentMapUnit.section_id)).tuples()) == {
                        (unit.unitId, unit.sectionId) for unit in payload.units
                    }
                else:
                    assert LexicalTransition._verifyPostgresql(session, binding, payload)["unitCount"] == len(payload.units)
        if outcome in ("failure", "interrupted"):
            assert matches(active_build_id, "quartzbaseline")
            assert not matches(active_build_id, "orchidenrichment")
            return
        assert matches(active_build_id, "orchidenrichment"), "The newly published scope is absent from OpenSearch"
        assert matches(active_build_id, "willowenrichment" if outcome == "concurrent" else "cedarcontinuity")
        assert not matches(active_build_id, "quartzbaseline")
        with Session(engine) as session:
            epoch: int = session.scalars(select(LexicalBackendControl.epoch)).one()
        operator = LexicalTransition(sessionFactory=sessionmaker(engine, expire_on_commit=False))
        if outcome == "abort":
            with Session(engine) as session, session.begin():
                result = session.get(JobResult, revision_id)
                assert result is not None
                result.document_metadata = {
                    **(result.document_metadata or {}),
                    "progressive": {"stages": {"finalization": {"status": "pending"}}},
                }
        operator.run("gate", targetBackend="postgresql", execute=True, expectedEpoch=epoch)
        if outcome == "abort":
            operator.run("abort", targetBackend="opensearch", execute=True, expectedEpoch=epoch + 1)
            with Session(engine) as session:
                control = session.get(LexicalBackendControl, 1)
                document = session.get(Document, document_id)
                assert control is not None and (control.backend, control.mode) == ("opensearch", "serving")
                assert document is not None and document.current_indexing_build_id == active_build_id
            evidence = asyncio.run(_discoverNative(namespace, query="orchidenrichment"))
            assert evidence and evidence[0]["content"] == "orchidenrichment"
            return
        operator.run("settle", targetBackend="postgresql", execute=True, expectedEpoch=epoch + 1)
        if outcome == "deferred":
            with monkeypatch.context() as offline:
                offline.setattr(settings, "KNOWHERE_OPENSEARCH_URL", "http://127.0.0.1:1")
                operator.run("backfill", targetBackend="postgresql", execute=True, expectedEpoch=epoch + 1)
        operator.run("resume", targetBackend="postgresql", execute=True, expectedEpoch=epoch + 1)
        with monkeypatch.context() as rollback:
            rollback.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "postgresql")
            pg_result = asyncio.run(_discoverNative(namespace, query="orchidenrichment"))
            assert pg_result and pg_result[0]["content"] == "orchidenrichment"
            assert not asyncio.run(_discoverNative(namespace, query="quartzbaseline"))
    finally:
        httpx.delete(f"{endpoint}/{native.indexName}", timeout=15, trust_env=False)
        native.close()
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


async def _discoverNative(namespace: str, *, query: str = "工程验收计划") -> list[dict[str, object]]:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from shared.services.retrieval.search.map_unit_discovery import map_unit_discovery
    engine = create_async_engine(get_contract_database_url())
    try:
        async with AsyncSession(engine) as session:
            result = await map_unit_discovery(session,
                user_id="native-contract-user", namespace=namespace,
                query=query, top_k=20, exclude_document_ids=[], exclude_sections=[],
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


def test_native_retrieval_finds_the_only_body_chunk_before_child_sections(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str = _getNativeEndpoint()
    query: str = "chickadee"
    parentContent: str = "The chickadee procedure applies to all subsections."
    childContent: str = "Check the archive window before maintenance."
    engine, jobId, namespace = _prepareParsedJob(monkeypatch, postgresql_proc, preparedChunks=[
        {"chunk_id": "parent-body", "type": "text", "content": parentContent,
         "path": "report.pdf/Field Guide", "order": 0, "metadata": {}},
        {"chunk_id": "child-body", "type": "text", "content": childContent,
         "path": "report.pdf/Field Guide/Procedure", "order": 1, "metadata": {}},
    ])
    from sqlalchemy.orm import sessionmaker
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.worker import IndexingWorker

    monkeypatch.setattr(LegacyCopyDispatch, "submit", staticmethod(lambda _build_id: False))
    index = OpenSearchLexicalIndex(endpoint=endpoint,
        scope=LexicalScope(ownerId="native-contract-user", namespace=namespace),
        indexPrefix="knowhere-slice-test")
    try:
        assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(jobId) == "completed"
        childRows: list[dict[str, object]] = asyncio.run(_discoverNative(namespace, query="archive"))
        assert [row["content"] for row in childRows] == [childContent]
        parentRows: list[dict[str, object]] = asyncio.run(_discoverNative(namespace, query=query))
        assert [row["content"] for row in parentRows] == [parentContent]
    finally:
        response: httpx.Response = httpx.delete(f"{endpoint}/{index.indexName}", timeout=15, trust_env=False)
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


async def _queryClassic(
    namespace: str, *, top_k: int = 1, use_agentic: bool | None = False,
) -> dict[str, object]:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from shared.services.retrieval.execution.plan import run_retrieval_query

    database = create_async_engine(get_contract_database_url())
    try:
        async with AsyncSession(database) as session:
            return await run_retrieval_query(db=session, user_id="native-contract-user", namespace=namespace,
                query="工程验收计划", top_k=top_k, use_agentic=use_agentic, exclude_document_ids=[], exclude_sections=[])
    finally:
        from shared.services.retrieval.lexical.asyncruntime import AsyncOpenSearchRuntime
        await AsyncOpenSearchRuntime.closeCurrent()
        await database.dispose()


@pytest.mark.parametrize("boundary", ("native-hits", "small-count", "small-admission"))
def test_native_fallback_keeps_one_publication(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import sessionmaker

    from shared.models.database.document import Document
    from shared.services.retrieval.execution import plan, routes
    from shared.services.retrieval.execution.route_types import RetrievalRouteContext, RetrievalRouteOutcome
    from shared.services.retrieval.hydration.build_snapshot import BuildSnapshot
    from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
    from shared.services.retrieval.lexical.hit import LexicalHit
    from shared.services.retrieval.lexical.progressivepublication import ProgressivePublication
    from shared.services.retrieval.lexical.reader import PublishedLexicalRead
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.publication_models import DocumentPublicationScope
    from shared.services.retrieval.publication_scope_content import replace_document_scope_content
    from shared.services.retrieval.routing.types import QueryMode, RouterDecision

    engine, namespace = classicRuntime
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as db:
        document = db.scalars(select(Document)).one()
        assert document.current_job_result_id and document.current_indexing_build_id
        scope = DocumentPublicationScope(
            user_id=document.user_id, namespace=namespace, document_id=document.document_id,
            job_result_id=document.current_job_result_id, source_file_name="report.pdf",
        )
        if boundary == "native-hits":
            BuildSnapshot.capture(
                db, scope=LexicalScope(ownerId=scope.user_id, namespace=namespace),
                buildId=document.current_indexing_build_id,
            )

    published: bool = False
    generations: list[int | None] = []

    def replace_selected_body(db: Session, staged: DocumentPublicationScope) -> None:
        replace_document_scope_content(db, scope=staged, scope_id="*", chunks=[
            {"chunk_id": "classic-first", "type": "text", "content": "工程验收计划 新正文", "path": "report.pdf/工程验收", "metadata": {}},
            {"chunk_id": "classic-second", "type": "text", "content": "项目质量管理程序", "path": "report.pdf/质量管理", "metadata": {}},
        ])

    def publish_once() -> None:
        nonlocal published
        if not published:
            ProgressivePublication(sessionFactory=sessions).publish(scope=scope, update=replace_selected_body)
            published = True

    if boundary == "native-hits":
        search = AsyncOpenSearchLexicalIndex.search

        async def search_then_publish(
            index: AsyncOpenSearchLexicalIndex, **options: Any,
        ) -> tuple[LexicalHit, ...]:
            hits = await search(index, **options)
            assert hits
            publish_once()
            return hits

        async def classify(_query: str) -> RouterDecision:
            return RouterDecision(
                mode=QueryMode.CLASSIC, reason="contract", source="llm",
                model_name="fixture", tokens_used=0,
            )

        monkeypatch.setattr(AsyncOpenSearchLexicalIndex, "search", search_then_publish)
        monkeypatch.setattr(routes.query_router_config, "QUERY_ROUTER_ENABLED", True)
        monkeypatch.setattr(routes, "classify_query_mode", classify)
    elif boundary == "small-count":
        count = routes.count_scoped_chunks

        async def count_then_publish(db: AsyncSession, **options: Any) -> int:
            count_result: int = await count(db, **options)
            publish_once()
            return count_result

        monkeypatch.setattr(routes, "count_scoped_chunks", count_then_publish)
    else:
        run_route = plan.run_retrieval_route

        async def publish_before_snapshot(context: RetrievalRouteContext) -> RetrievalRouteOutcome:
            reader = PublishedLexicalRead.current()
            assert reader is not None
            generations.append(reader.pins.generation)
            publish_once()
            return await run_route(context)

        monkeypatch.setattr(plan, "run_retrieval_route", publish_before_snapshot)

    query_options: dict[str, Any] = (
        {"top_k": 1, "use_agentic": None} if boundary == "native-hits"
        else {"top_k": 2, "use_agentic": True}
    )
    response: dict[str, object] = asyncio.run(_queryClassic(namespace, **query_options))
    results: object = response["results"]
    assert isinstance(results, list)
    assert published
    bodies: list[str] = [row["content"] for row in results if row["chunk_id"] == "classic-first"]
    if boundary == "small-admission":
        assert len(generations) == 2 and generations[0] != generations[1]
        assert bodies == ["工程验收计划 新正文"]
    else:
        assert bodies == ["工程验收计划"]
        fresh_results: object = asyncio.run(_queryClassic(namespace, **query_options))["results"]
        assert isinstance(fresh_results, list)
        assert [row["content"] for row in fresh_results if row["chunk_id"] == "classic-first"] == ["工程验收计划 新正文"]
    assert response["router_used"] == ("classic_topk" if boundary == "native-hits" else "small_corpus_all")


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


def test_classic_read_returns_evidence_while_chunk_final_claim_is_locked(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.schemas.progressive_ingestion import ProgressiveIngestionState
    from shared.services.jobs.lifecycle import scope_enrichments
    from shared.services.jobs.progressive_ingestion import read_progressive_state, write_progressive_state
    from shared.services.retrieval.execution import plan
    from shared.services.retrieval.lexical.asyncruntime import AsyncOpenSearchRuntime

    engine, namespace = classicRuntime
    with Session(engine) as database, database.begin():
        document = database.scalars(select(Document)).one()
        revision = database.get(JobResult, document.current_job_result_id)
        assert revision is not None
        revision_id: str = revision.id
        job = database.get(Job, revision.job_id)
        assert job is not None
        job.job_metadata = {**(job.job_metadata or {}), "parse_track": "chunk",
                            "ingestion_mode": "progressive", "enrichment_status": "text"}
        revision.document_metadata = write_progressive_state(
            {**(revision.document_metadata or {}), "ingestion_mode": "progressive",
             "enrichment_status": "text"},
            ProgressiveIngestionState(stages={"finalization": {"status": "pending"}}),
        )

    dispatched: list[str] = []

    def dispatch(revision_id: str) -> bool:
        dispatched.append(revision_id)
        return True

    monkeypatch.setattr(scope_enrichments, "enqueue_chunk_final_after_commit", dispatch)

    async def exercise() -> None:
        database = create_async_engine(get_contract_database_url())
        claim_started = asyncio.Event()

        @event.listens_for(database.sync_engine, "before_cursor_execute")
        def observe_claim(_connection: Connection, _cursor: object, statement: str,
                          _parameters: object, _context: object, _executemany: bool) -> None:
            if "FOR UPDATE OF job_results" in statement:
                claim_started.set()

        async def search() -> dict[str, object]:
            async with AsyncSession(database) as reader:
                return await plan.run_retrieval_query(
                    db=reader, user_id="native-contract-user", namespace=namespace,
                    query="工程验收计划", top_k=1, use_agentic=False,
                    exclude_document_ids=[], exclude_sections=[],
                )

        def assert_evidence(response: dict[str, object]) -> None:
            assert response["router_used"] == "classic_topk"
            results: object = response["results"]
            assert isinstance(results, list) and results
            assert results[0]["content"] == "工程验收计划"
            assert results[0]["source"]["job_result_id"] == revision_id

        try:
            async with AsyncSession(database) as blocker:
                await blocker.execute(
                    select(JobResult.id).where(JobResult.id == revision_id).with_for_update()
                )
                request = asyncio.create_task(search())
                try:
                    await asyncio.wait_for(claim_started.wait(), timeout=10)
                    assert_evidence(await asyncio.wait_for(asyncio.shield(request), timeout=3))
                finally:
                    await blocker.rollback()
                    if not request.done():
                        request.cancel()
                        await asyncio.gather(request, return_exceptions=True)
                assert dispatched == []
                stored = await blocker.get(JobResult, revision_id)
                assert stored is not None
                state = read_progressive_state(stored.document_metadata)
                assert state is not None and state.stages["finalization"]["status"] == "pending"
                await blocker.rollback()

            for response in await asyncio.gather(search(), search()):
                assert_evidence(response)
            assert dispatched == [revision_id]
        finally:
            await AsyncOpenSearchRuntime.closeCurrent()
            await database.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("publication_count", [0, 1, 2])
@pytest.mark.parametrize("boundary", ["assembled", "hydration"])
def test_progressive_native_read_returns_available_evidence(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch, publication_count: int,
    boundary: Literal["assembled", "hydration"],
) -> None:
    from shared.models.database.document import Document
    from shared.models.database.job_result import JobResult
    from shared.services.retrieval.execution import plan
    from shared.services.retrieval.execution.route_types import RetrievalRouteContext, RetrievalRouteOutcome
    from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    from shared.services.retrieval.lexical.hit import LexicalHit
    from shared.services.retrieval.lexical.progressivepublication import ProgressivePublication
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.publication_models import DocumentPublicationScope
    from shared.services.retrieval.publication_scope_content import replace_document_scope_content
    from sqlalchemy.orm import sessionmaker

    engine, namespace = classicRuntime
    with Session(engine) as database, database.begin():
        document = database.scalars(select(Document)).one()
        revision = database.get(JobResult, document.current_job_result_id)
        assert revision is not None
        revision.document_metadata = {**(revision.document_metadata or {}), "ingestion_mode": "progressive"}
        scope = DocumentPublicationScope(user_id=document.user_id, namespace=namespace,
            document_id=document.document_id, job_result_id=revision.id, source_file_name="report.pdf")

    original = plan.run_retrieval_route
    read_generations: list[int | None] = []
    publications: list[int] = []
    revised_content: str = "工程验收计划 细化内容"
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(LegacyCopyDispatch, "submit", staticmethod(lambda identity: False))

    def publish_refinement(db: Session, staged_scope: DocumentPublicationScope) -> None:
        chapter = f"细化{len(publications)}" if boundary == "hydration" else "工程验收"
        replace_document_scope_content(db, scope=staged_scope, scope_id="*", chunks=[
            {"chunk_id": "refined-first", "type": "text", "content": revised_content,
                "path": f"report.pdf/{chapter}", "metadata": {}},
            {"chunk_id": "classic-second", "type": "text", "content": "项目质量管理程序",
                "path": "report.pdf/质量管理", "metadata": {}},
        ])

    def publish_once() -> None:
        publications.append(len(publications) + 1)
        ProgressivePublication(sessionFactory=sessions).publish(scope=scope, update=publish_refinement)

    original_search = AsyncOpenSearchLexicalIndex.search

    async def publish_before_hydration(self: AsyncOpenSearchLexicalIndex, **options: Any) -> tuple[LexicalHit, ...]:
        hits = await original_search(self, **options)
        if len(publications) < publication_count:
            assert hits
            publish_once()
        return hits

    async def publish_after_retrieval(context: RetrievalRouteContext) -> RetrievalRouteOutcome:
        outcome = await original(context)
        pins = context.revision_pins
        assert pins is not None
        read_generations.append(pins.generation)
        if publication_count == 0 and len(read_generations) == 1:
            replacement_id = _stageReplacement(engine, namespace)
            assert IndexingWorker(sessionFactory=sessions).run(replacement_id) == "completed"
        elif boundary == "assembled" and len(publications) < publication_count:
            publish_once()
        return outcome

    monkeypatch.setattr(plan, "run_retrieval_route", publish_after_retrieval)
    if boundary == "hydration":
        monkeypatch.setattr(AsyncOpenSearchLexicalIndex, "search", publish_before_hydration)
    response = asyncio.run(_queryClassic(namespace))
    results: object = response["results"]
    assert isinstance(results, list)
    if boundary == "assembled" or publication_count == 0:
        assert len(read_generations) == 1
        assert results and results[0]["content"] == "工程验收计划"
        assert results[0]["source"]["job_result_id"] == scope.job_result_id
    else:
        assert len(read_generations) == 2
        assert read_generations[0] is not None and read_generations[1] == read_generations[0] + 1
        if publication_count == 1:
            assert results and results[0]["content"] == revised_content
            assert results[0]["source"]["job_result_id"] == scope.job_result_id
        else:
            assert results == []


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


def test_manifest_query_keeps_the_body_from_the_selected_build(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.orm import sessionmaker

    from shared.core.config import settings
    from shared.models.database.document import Document
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.lexicalhydrationsnapshot import LexicalHydrationSnapshot
    from shared.models.database.lexicalpublishedmanifest import LexicalPublishedManifest
    from shared.services.retrieval.lexical.cleanup import LexicalCleanup
    from shared.services.retrieval.lexical.manifest_maintenance import collectPublishedManifests
    from shared.services.retrieval.lexical.asyncnative import AsyncOpenSearchLexicalIndex
    from shared.services.retrieval.lexical.hit import LexicalHit
    from shared.services.retrieval.lexical.manifest_publication import ManifestPublication
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.progressivepublication import ProgressivePublication
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.publication_models import DocumentPublicationScope
    from shared.services.retrieval.publication_scope_content import replace_document_scope_content

    engine, namespace = classicRuntime
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        document = session.scalars(select(Document)).one()
        assert document.current_job_result_id is not None and document.current_indexing_build_id is not None
        oldBuild = document.current_indexing_build_id
        scope = DocumentPublicationScope(user_id=document.user_id, namespace=namespace,
            document_id=document.document_id, job_result_id=document.current_job_result_id, source_file_name="report.pdf")
    lexicalScope = LexicalScope(ownerId=scope.user_id, namespace=namespace)
    native = OpenSearchLexicalIndex(endpoint=_getNativeEndpoint(), scope=lexicalScope,
        indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX)
    native.createManifestIndex()
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_PUBLISHED_MANIFESTS", True)
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_MANIFEST_LOOKUP", True)
    firstManifest = ManifestPublication(sessions).seed(scope=lexicalScope, native=native)
    original = AsyncOpenSearchLexicalIndex.search
    observed: list[str] = []

    def publish(db: Session, staged: DocumentPublicationScope) -> None:
        replace_document_scope_content(db, scope=staged, scope_id="*", chunks=[
            {"chunk_id": "new-first", "type": "text", "content": "工程验收计划 新正文", "path": "report.pdf/工程验收", "metadata": {}},
            {"chunk_id": "classic-second", "type": "text", "content": "项目质量管理程序", "path": "report.pdf/质量管理", "metadata": {}},
        ])

    async def refineAfterSearch(self: AsyncOpenSearchLexicalIndex, **options: Any) -> tuple[LexicalHit, ...]:
        lookup = options["buildLookup"]
        hits = await original(self, **options)
        assert hits
        observed.append(lookup["id"])
        if len(observed) == 1:
            ProgressivePublication(sessionFactory=sessions).publish(scope=scope, update=publish)
        return hits

    monkeypatch.setattr(AsyncOpenSearchLexicalIndex, "search", refineAfterSearch)
    try:
        before = asyncio.run(_queryClassic(namespace))
        after = asyncio.run(_queryClassic(namespace))
        assert isinstance(before["results"], list) and before["results"][0]["content"] == "工程验收计划"
        assert isinstance(after["results"], list) and after["results"][0]["content"] == "工程验收计划 新正文"
        assert observed[0] == firstManifest and observed[1] != firstManifest
        cleanup = LexicalCleanup(sessionFactory=sessions)
        assert not cleanup.collect(oldBuild)
        assert collectPublishedManifests(sessions) == 0
        with sessions.begin() as session:
            build = session.get(LexicalBuild, oldBuild)
            manifest = session.get(LexicalPublishedManifest, firstManifest)
            assert build is not None and build.manifestCleanupAfter is not None
            assert manifest is not None and manifest.cleanupAfter is not None
            due = build.manifestCleanupAfter - timedelta(minutes=5)
            build.manifestCleanupAfter = due
            manifest.cleanupAfter = due
        assert cleanup.collect(oldBuild)
        assert collectPublishedManifests(sessions) == 1
        with sessions.begin() as session:
            assert session.get(LexicalHydrationSnapshot, oldBuild) is not None
            assert session.get(LexicalPublishedManifest, firstManifest) is None
    finally:
        native.close()
        response = httpx.delete(f"{_getNativeEndpoint()}/{native.manifestIndexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)


def test_parallel_document_refinements_rebase_the_published_manifest(
    classicRuntime: tuple[Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Barrier, Lock

    from sqlalchemy.orm import sessionmaker

    from shared.core.config import settings
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
    from shared.services.retrieval.lexical.manifest_publication import ManifestPublication, PreparedManifest
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.progressivepublication import ProgressivePublication
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.publication_models import DocumentPublicationScope
    from shared.services.retrieval.publication_scope_content import replace_document_scope_content

    engine, namespace = classicRuntime
    sessions = sessionmaker(engine, expire_on_commit=False)
    userId = "native-contract-user"
    lexicalScope = LexicalScope(ownerId=userId, namespace=namespace)
    native = OpenSearchLexicalIndex(endpoint=_getNativeEndpoint(), scope=lexicalScope,
        indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX)
    native.createManifestIndex()
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_PUBLISHED_MANIFESTS", True)
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_PG_COPY_ON_DEMAND", True)
    firstManifest = ManifestPublication(sessions).seed(scope=lexicalScope, native=native)
    secondJob = "native-contract-second"
    try:
        with sessions.begin() as session:
            session.add(Job(job_id=secondJob, user_id=userId, job_type="document_ingestion",
                source_type="file", status="running", job_metadata={
                    "document_id": "doc_native_second", "namespace": namespace, "source_file_name": "report.pdf",
                }))
        with sessions.begin() as session:
            finalized = SyncJobSuccessFinalizer().finalize(session, job_id=secondJob,
                result_s3_key="results/second.zip", checksum="second", zip_size=10,
                chunks=[{"chunk_id": "second-original", "type": "text", "content": "second baseline",
                    "path": "report.pdf/Chapter", "metadata": {}}], stored_count=1,
                delivery_mode="url", section_summaries=None)
            assert finalized.response.should_commit()
        assert IndexingWorker(sessionFactory=sessions).run(secondJob) == "completed"
        with sessions.begin() as session:
            documents = list(session.scalars(select(Document).where(Document.namespace == namespace)))
            assert len(documents) == 2
            previousBuilds = {document.document_id: document.current_indexing_build_id for document in documents}
            scopes = [DocumentPublicationScope(user_id=userId, namespace=namespace,
                document_id=document.document_id, job_result_id=str(document.current_job_result_id),
                source_file_name="report.pdf") for document in documents]

        barrier = Barrier(2, timeout=10)
        guard = Lock()
        attempts: list[tuple[str | None, int]] = []
        prepare = ManifestPublication.prepare

        def synchronize(self: ManifestPublication, **options: Any) -> PreparedManifest | None:
            candidate = prepare(self, **options)
            assert candidate is not None
            with guard:
                attempts.append((candidate.previousId, candidate.generation))
                isFirstRound = len(attempts) <= 2
            if isFirstRound:
                barrier.wait()
            return candidate

        def publish(scope: DocumentPublicationScope) -> None:
            def update(db: Session, staged: DocumentPublicationScope) -> None:
                replace_document_scope_content(db, scope=staged, scope_id="*", chunks=[
                    {"chunk_id": "refined-" + scope.document_id, "type": "text",
                     "content": "工程验收计划", "path": "report.pdf/Refined", "metadata": {}},
                ])
            ProgressivePublication(sessionFactory=sessions).publish(scope=scope, update=update)

        monkeypatch.setattr(ManifestPublication, "prepare", synchronize)
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(publish, scopes))
        assert len(attempts) == 3 and attempts[0] == attempts[1] and attempts[1] != attempts[2]
        with sessions.begin() as session:
            registry = session.get(LexicalNamespaceIndex, lexicalScope.fingerprint)
            assert registry is not None and registry.publishedManifestId is not None
            currentManifest = registry.publishedManifestId
            currentBuilds = {
                document.document_id: document.current_indexing_build_id
                for document in session.scalars(select(Document).where(Document.namespace == namespace))
            }
        assert currentBuilds.keys() == previousBuilds.keys()
        assert all(build != previousBuilds[document] for document, build in currentBuilds.items())
        response = httpx.get(f"{_getNativeEndpoint()}/{native.manifestIndexName}/_doc/{currentManifest}",
            timeout=15, trust_env=False).raise_for_status().json()
        assert set(response["_source"]["buildIds"]) == set(currentBuilds.values())
        assert httpx.get(f"{_getNativeEndpoint()}/{native.manifestIndexName}/_doc/{firstManifest}",
            timeout=15, trust_env=False).status_code == 200
    finally:
        native.close()
        response = httpx.delete(f"{_getNativeEndpoint()}/{native.manifestIndexName}", timeout=15, trust_env=False)
        assert response.status_code in (200, 404)
