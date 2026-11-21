"""Exercise immutable enrichment bodies against the migrated database."""

import asyncio
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shared.models.database.document import Document, DocumentChunk, DocumentSection
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.testing.contract_runtime import (
    PostgreSQLProcess,
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


def test_same_source_path_keeps_independent_chunk_bodies(
    monkeypatch: Any, postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    from shared.core.config import settings
    monkeypatch.setitem(settings.__dict__, "KNOWHERE_ADDITIVE_PUBLICATION_ENABLED", True)
    monkeypatch.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "opensearch")
    asyncio.run(prepare_contract_storage())
    engine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))
    fixture = PublicationScope(
        user_ref="additive-publication-user",
        namespace_ref="additive-publication",
        source_file_name="same-file.pdf",
        job_ref="additive-publication-job",
        revision_ref="additive-publication-result",
    )
    from shared.models.database.document_enrichment_publication import DocumentEnrichmentPublication
    from shared.services.retrieval.lexical.additivepublication import AdditivePublication

    try:
        with Session(engine) as db, db.begin():
            seed_publication_fixture(db, scope=fixture)
            document = Document(
                document_id="additive-publication-doc", user_id=fixture.user_ref,
                namespace=fixture.namespace_ref, status="active", source_file_name=fixture.source_file_name,
                accepted_job_id=fixture.job_ref, current_job_result_id=fixture.revision_ref,
                current_indexing_build_id="original-build",
            )
            db.add(document)
            db.flush()
            scope = DocumentPublicationScope(
                user_id=fixture.user_ref, namespace=fixture.namespace_ref,
                source_file_name=fixture.source_file_name, document_id=document.document_id,
                job_result_id=fixture.revision_ref,
            )
            path = "same-file.pdf/Heading"
            db.add(DocumentSection(
                section_id="original-heading", section_title="Heading", section_path="Heading",
                document_id=scope.document_id, user_id=scope.user_id, namespace=scope.namespace,
                job_result_id=scope.job_result_id,
            ))
            db.flush()
            db.add(DocumentChunk(
                id="original-row", section_id="original-heading", chunk_id="original", document_id=scope.document_id,
                user_id=scope.user_id, namespace=scope.namespace, job_result_id=scope.job_result_id,
                source_chunk_path=path, chunk_type="page", content="Baseline spans original pages 1 through 3.",
                chunk_metadata={
                    "page_nums": [1, 2, 3], "scope_id": "p1-3",
                    "page_texts": [{"page_num": 1, "text": "Original physical page 1"}],
                    "assets": [{"asset_id": "original-recording", "kind": "audio", "origin": "embedded",
                        "page_nums": [1], "ref": {"kind": "artifact", "path": "embedded_media/recording.m4a"}}],
                },
            ))
            db.add(DocumentChunk(
                id="original-recording-row", chunk_id="original-recording", document_id=scope.document_id,
                user_id=scope.user_id, namespace=scope.namespace, job_result_id=scope.job_result_id,
                source_chunk_path="same-file.pdf/Recording", chunk_type="audio", content="Original recording",
                file_path="embedded_media/recording.m4a",
            ))
            db.flush()
            payload: list[dict[str, Any]] = [{
                "chunk_id": "fine", "path": path, "type": "page", "content": "Enriched page 1.",
                "metadata": {"page_nums": [1]},
            }]
            AdditivePublication.storeBody(db, scope=scope, scopeId="p1-3", attemptId="attempt-one",
                chunks=payload, rawPrefix="retained/enrichment")
        with Session(engine) as db, db.begin():
            stored = db.get(DocumentEnrichmentPublication, "attempt-one")
            assert stored is not None
            assert stored.state == "writing"
            assert stored.raw_prefix == "retained/enrichment"
            assert stored.expected_index_count is None
            AdditivePublication.fail(db, scope=scope, attemptId="attempt-one")
            AdditivePublication.storeBody(db, scope=scope, scopeId="p1-3", attemptId="attempt-two",
                chunks=[{**payload[0], "content": "Replacement enrichment."}])
            bodies = db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == scope.document_id,
                DocumentChunk.chunk_type != "audio")).all()
            assert {row.enrichment_attempt_id: str(row.content or "").splitlines()[0] for row in bodies} == {
                None: "Baseline spans original pages 1 through 3.",
                "attempt-one": "Enriched page 1.",
                "attempt-two": "Replacement enrichment.",
            }
            assert {row.source_chunk_path for row in bodies} == {path}
            assert len({row.id for row in bodies}) == 3
            assert len({row.chunk_id for row in bodies}) == 3
            retained_id = next(row.id for row in bodies if row.enrichment_attempt_id == "attempt-one")
            assert stored.state == "failed"
            assert not AdditivePublication.complete(db, scope=scope, attemptId="attempt-one")
            AdditivePublication.storeBody(db, scope=scope, scopeId="p5-7", attemptId="attempt-hidden",
                chunks=[{**payload[0], "path": "same-file.pdf/Hidden", "content": "Fine failed summary",
                    "metadata": {"page_nums": [9]}}],
                sectionSummaries={"Hidden": "Fine failed summary"})
            AdditivePublication.fail(db, scope=scope, attemptId="attempt-hidden")
        from sqlalchemy.orm import sessionmaker

        sessions = sessionmaker(engine)
        with sessions.begin() as db:
            completed = db.get(DocumentEnrichmentPublication, "attempt-two")
            assert completed is not None
            completed.expected_index_count = 1
            assert AdditivePublication.complete(db, scope=scope, attemptId="attempt-two")
        from shared.services.retrieval.page_embedding_index import load_missing_document_page_embedding_pages

        with sessions.begin() as db:
            assert load_missing_document_page_embedding_pages(db, scope=scope) == [1, 2, 3]

        retained_inputs = AdditivePublication.preparePublishedInputs(sessions, scope=scope)
        assert len(retained_inputs) == 1
        identity, units = retained_inputs[0]
        assert identity == "attempt-two"
        assert len(units) == 1 and units[0].sectionPath == "Heading"

        async def assert_candidates() -> None:
            from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
            from shared.services.retrieval.lexical.additivevisibility import AdditiveVisibility
            from shared.services.retrieval.lexical.scope import LexicalScope

            async_engine = create_async_engine(make_url(get_contract_database_url()).set(drivername="postgresql+asyncpg"))
            try:
                async with AsyncSession(async_engine) as db:
                    candidates = tuple((scope.document_id, scope.job_result_id, build) for build in (
                        "original-build", "unpublished-build", "attempt-one", "attempt-two",
                    ))
                    lexical_scope = LexicalScope(ownerId=scope.user_id, namespace=scope.namespace)
                    eligible = await AdditiveVisibility.filterCandidates(db, lexical_scope, candidates)
                    assert eligible == {candidates[0], candidates[3]}
                    from app.repositories.document_repository import DocumentRepository
                    repository = DocumentRepository()
                    assert await repository.count_current_document_chunks(
                        db, document_id=scope.document_id, job_result_id=scope.job_result_id,
                    ) == 3
                    from shared.services.retrieval.hydration.path import hydrate_paths_to_rows

                    outline = await hydrate_paths_to_rows(db, user_id=scope.user_id, namespace=scope.namespace,
                        path_selections=[{"path": "Hidden", "hydrate_mode": "outline"}])
                    assert not outline

                    from shared.services.retrieval.agent_tools.tools.read import _resolve_same_as_markers

                    marker = {"document_id": scope.document_id, "chunk_type": "page",
                        "content": "[SAME-AS same-file.pdf/Heading p1]", "chunk_metadata": {"enrichment_state": "refined"}}
                    shared = [{**marker, "enrichment_attempt_id": attempt_id} for attempt_id in ("attempt-two", "neighbor")]
                    await _resolve_same_as_markers(db, shared, revision_by_doc={scope.document_id: scope.job_result_id},
                        source_file_name_by_doc={scope.document_id: scope.source_file_name or ""})
                    assert "Replacement enrichment." in shared[0]["content"]
                    assert "Original physical page 1" in shared[1]["content"]
                    assert "Replacement enrichment." not in shared[1]["content"]

                    from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
                    from shared.services.retrieval.lexical.hit import LexicalHit
                    enriched = await AdditiveVisibility.loadRows(db, scope=lexical_scope, hits=(
                        LexicalHit(buildId=identity, unit=units[0], score=1.0),
                    ))
                    connected = await hydrate_connected_target_rows(db=db, rows=enriched,
                        exclude_document_ids=[], exclude_sections=[], lexicalScope=lexical_scope)
                    assert [(row["chunk_id"], row["source_row_id"], row["build_id"]) for row in connected] == [
                        ("original-recording", "original-recording-row", "attempt-two"),
                    ]
                from app.services.documents.lifecycle_service import DocumentService
                from shared.services.retrieval.hydration.reference import hydrate_referenced_chunk_rows

                service = DocumentService()
                async with AsyncSession(async_engine, expire_on_commit=False) as db:
                    refs = [{"document_id": scope.document_id, "chunk_id": "original"}]
                    pins = {scope.document_id: scope.job_result_id}
                    assert await hydrate_referenced_chunk_rows(db=db, user_id=scope.user_id,
                        namespace=scope.namespace, refs=refs, revision_pins=pins)
                    archived = await service.archive_document(db, user_id=scope.user_id, document_id=scope.document_id)
                    assert archived is not None and archived["status"] == "archived"

                async with AsyncSession(async_engine) as db:
                    assert not await AdditiveVisibility.filterCandidates(db, lexical_scope, candidates)
                    assert not await hydrate_referenced_chunk_rows(db=db, user_id=scope.user_id,
                        namespace=scope.namespace, refs=refs, revision_pins=pins)
                    retained = await service.get_document_chunk(db, user_id=scope.user_id,
                        document_id=scope.document_id, job_result_id=scope.job_result_id,
                        document_chunk_id=retained_id, include_asset_urls=False)
                    assert retained is not None and retained["chunk"]["content"].splitlines()[0] == "Enriched page 1."
                    document = await db.get(Document, scope.document_id)
                    assert document is not None and document.index_cleanup_after is not None
            finally:
                await async_engine.dispose()

        asyncio.run(assert_candidates())
    finally:
        engine.dispose()


def test_native_partial_enrichment_is_eligible_until_failed(
    monkeypatch: Any, postgresql_proc: PostgreSQLProcess, record_property: Any,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    import os
    from threading import Event
    from time import monotonic
    from uuid import uuid4

    import httpx
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    endpoint = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if endpoint is None:
        pytest.skip("Set KNOWHERE_TEST_OPENSEARCH_URL to an isolated test node")

    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())

    from app.services.documents.lifecycle_service import DocumentService
    from shared.core.config import settings
    from shared.services.retrieval.document_scope import DocumentScope
    from shared.services.retrieval.lexical import additivepublication
    from shared.services.retrieval.lexical.additivecleanup import AdditiveCleanup
    from shared.services.retrieval.lexical.coordination import LexicalCoordination
    from shared.services.retrieval.lexical.discovery import discoverOpenSearch
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.models.database.document_enrichment_publication import DocumentEnrichmentPublication
    from shared.models.database.job import Job
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.transition import LexicalTransition, _Binding
    from shared.services.retrieval.lexical.unit import LexicalUnit
    from shared.services.retrieval.serving_generation import lock_namespace_generation

    monkeypatch.setitem(settings.__dict__, "KNOWHERE_ADDITIVE_PUBLICATION_ENABLED", True)
    monkeypatch.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_URL", endpoint)
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_INDEX_PREFIX", "knowhere-additive-test")
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_BULK_MAXIMUM_BYTES", 900)
    suffix = uuid4().hex[:10]
    fixture = PublicationScope(
        user_ref=f"additive-user-{suffix}", namespace_ref=f"partial-{suffix}", source_file_name="chapter.pdf",
        job_ref=f"partial-job-{suffix}", revision_ref=f"partial-result-{suffix}",
    )
    scope = DocumentPublicationScope(user_id=fixture.user_ref, namespace=fixture.namespace_ref,
        source_file_name=fixture.source_file_name, document_id=f"doc_{suffix}", job_result_id=fixture.revision_ref)
    native_scope = LexicalScope(ownerId=scope.user_id, namespace=scope.namespace)
    native = OpenSearchLexicalIndex(endpoint=endpoint, scope=native_scope, indexPrefix=settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX)
    engine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))
    sessions = sessionmaker(engine)
    monkeypatch.setattr(additivepublication, "get_sync_session_factory", lambda: sessions)
    baseline_build = f"initial-{suffix}"
    attempt = f"attempt-{suffix}"
    section_id = f"section-{suffix}"
    common = {"document_id": scope.document_id, "user_id": scope.user_id, "namespace": scope.namespace,
        "job_result_id": scope.job_result_id}

    async def discover(query: str) -> list[dict[str, Any]]:
        async_engine = create_async_engine(get_contract_database_url())
        try:
            async with AsyncSession(async_engine) as db:
                result = await discoverOpenSearch(db, userId=scope.user_id, namespace=scope.namespace, query=query,
                    topK=8, documentScope=DocumentScope(), excludeDocumentIds=[], excludeSections=[],
                    chunkTypes=None, signalPaths=None, filterMode="delete", revisionPins=None, sectionTargets=None)
                return result.payload["fused_rows"]
        finally:
            await async_engine.dispose()

    class InterruptedWrite(Exception):
        pass

    callbacks = 0
    writer_paused = Event()
    release_writer = Event()
    neighbor_attempt = f"neighbor-{suffix}"
    initial_job = f"initial-job-{suffix}"
    initial_document = f"doc_initial_{suffix}"

    def pauseAfterFirstBatch() -> None:
        nonlocal callbacks
        callbacks += 1
        if callbacks != 3:
            return
        writer_paused.set()
        if not release_writer.wait(25):
            raise TimeoutError("Partial writer was never released by the test")
        raise InterruptedWrite()

    def publishNeighbor() -> float:
        start = monotonic()
        with sessions.begin() as db:
            additivepublication.AdditivePublication.storeBody(db, scope=scope, scopeId="p3-4",
                attemptId=neighbor_attempt, chunks=[{
                    "chunk_id": "neighbor", "type": "page", "path": "chapter.pdf/Independent",
                    "content": "Independent sibling chapter under the same document",
                    "metadata": {"page_nums": [3, 4]},
                }])
        additivepublication.AdditivePublication.writeIndex(scope=scope, attemptId=neighbor_attempt)
        with sessions.begin() as db:
            assert additivepublication.AdditivePublication.complete(db, scope=scope, attemptId=neighbor_attempt)
        return monotonic() - start

    def publishInitialDocument() -> float:
        start = monotonic()
        assert IndexingWorker(sessionFactory=sessions).run(initial_job) == "completed"
        return monotonic() - start

    async def repeatFreshReads() -> tuple[list[dict[str, Any]], list[float]]:
        semaphore = asyncio.Semaphore(4)

        async def read_once() -> tuple[list[dict[str, Any]], float]:
            async with semaphore:
                start = monotonic()
                rows = await discover("climate")
                return rows, monotonic() - start

        read_windows = await asyncio.gather(*(read_once() for _ in range(12)))
        for rows, _ in read_windows:
            assert {row["build_id"] for row in rows} == {baseline_build, attempt}
            assert len([row for row in rows if row["build_id"] == attempt]) == 1
        return read_windows[0][0], [duration for _, duration in read_windows]

    try:
        LexicalCoordination(sessions).ensureIndex(scope=native_scope, native=native)
        with sessions.begin() as db:
            seed_publication_fixture(db, scope=fixture)
            db.add(Document(document_id=scope.document_id, user_id=scope.user_id, namespace=scope.namespace,
                source_file_name=scope.source_file_name, current_job_result_id=scope.job_result_id,
                current_indexing_build_id=baseline_build))
            db.flush()
            db.add(DocumentSection(section_id=section_id, section_path="Main", section_title="Main", **common))
            db.flush()
            db.add(DocumentChunk(id=f"coarse-{suffix}", chunk_id=f"coarse-{suffix}", section_id=section_id,
                chunk_type="page", content="Climate chapter baseline", source_chunk_path="chapter.pdf/Main",
                chunk_metadata={"page_nums": [1, 2], "scope_id": "p1-2", "enrichment_state": "coarse"}, **common))
            db.flush()
            additivepublication.AdditivePublication.storeBody(db, scope=scope, scopeId="p1-2", attemptId=attempt, chunks=[
                {"chunk_id": "main", "type": "page", "path": "chapter.pdf/Main", "content": "climate detail " * 32,
                    "metadata": {"page_nums": [1]}},
                {"chunk_id": "secondary", "type": "page", "path": "chapter.pdf/Secondary", "content": "climate appendix " * 28,
                    "metadata": {"page_nums": [2]}},
            ])
        native.writeBuild(buildId=baseline_build, units=(LexicalUnit(
            documentId=scope.document_id, revisionId=scope.job_result_id, sectionId=section_id,
            unitId=section_id, sectionPath="Main", contentTokens="climate chapter baseline",
        ),))
        with sessions.begin() as db:
            db.add(Job(job_id=initial_job, user_id=scope.user_id, job_type="document_ingestion",
                status="running", source_type="file", job_metadata={
                    "document_id": initial_document, "namespace": scope.namespace,
                    "source_file_name": "independent.txt", "parse_track": "chunk", "ingestion_mode": "full",
                }))
        with sessions.begin() as db:
            parsed = SyncJobSuccessFinalizer().finalize(db, job_id=initial_job,
                result_s3_key=f"results/{initial_job}/result.zip", checksum="fixture-checksum",
                zip_size=10, stored_count=1, delivery_mode="url", section_summaries=None, chunks=[{
                    "chunk_id": f"initial-body-{suffix}", "type": "text",
                    "path": "independent.txt/Induction", "content": "Induction entered the namespace while A was paused.",
                    "metadata": {},
                }])
            assert parsed.response.should_commit()
        with sessions.begin() as db:
            saved_generation = lock_namespace_generation(db, user_id=scope.user_id, namespace=scope.namespace).generation
        with ThreadPoolExecutor(max_workers=4) as executor, sessions.begin() as hold:
            generation = lock_namespace_generation(hold, user_id=scope.user_id, namespace=scope.namespace)
            writer = executor.submit(additivepublication.AdditivePublication.writeIndex, scope=scope,
                attemptId=attempt, assertOwned=pauseAfterFirstBatch)
            try:
                assert writer_paused.wait(15), "Writer A did not put its first batch on the network"
                assert native.countBuild(attempt) == 1
                reads = executor.submit(asyncio.run, repeatFreshReads())
                neighbor = executor.submit(publishNeighbor)
                initial = executor.submit(publishInitialDocument)
                partial_rows, latencies = reads.result(timeout=12)
                neighbor_time = neighbor.result(timeout=12)
                initial_time = initial.result(timeout=12)
                assert not writer.done(), "Independent work completed only after Writer A resumed"
                assert sorted(row["build_id"] for row in asyncio.run(discover("independent"))) == [neighbor_attempt]
                assert {row["document_id"] for row in asyncio.run(discover("induction"))} == {initial_document}
                assert generation.generation == saved_generation
                first_row_id = str(next(row["source_row_id"] for row in partial_rows if row["build_id"] == attempt))
                record_property("fresh_reads_during_partial_bulk_and_generation_lock", len(latencies))
                record_property("fresh_read_max_seconds", round(max(latencies), 4))
                record_property("same_document_neighbor_publication_seconds", round(neighbor_time, 4))
                record_property("same_namespace_initial_publication_seconds", round(initial_time, 4))
            finally:
                release_writer.set()
            with pytest.raises(InterruptedWrite):
                writer.result(timeout=12)
        with sessions.begin() as db:
            assert lock_namespace_generation(db, user_id=scope.user_id, namespace=scope.namespace).generation == saved_generation
        assert not additivepublication.AdditivePublication.hasCompleteIndex(scope=scope, attemptId=attempt)
        with sessions.begin() as db:
            additivepublication.AdditivePublication.fail(db, scope=scope, attemptId=attempt)
        assert {row["build_id"] for row in asyncio.run(discover("climate"))} == {baseline_build}
        assert native.countBuild(attempt) == 1
        late_units = LexicalTransition._readNativeUnits(native, attempt)
        assert len(late_units) == 1

        async def readRetained(identity: str) -> dict[str, Any] | None:
            async_engine = create_async_engine(get_contract_database_url())
            try:
                async with AsyncSession(async_engine) as db:
                    return await DocumentService().get_document_chunk(db, user_id=scope.user_id,
                        document_id=scope.document_id, job_result_id=scope.job_result_id,
                        document_chunk_id=identity, include_asset_urls=False)
            finally:
                await async_engine.dispose()

        retained = asyncio.run(readRetained(first_row_id))
        assert retained and "climate" in retained["chunk"]["content"]
        assert additivepublication.AdditivePublication.cleanup(scope=scope, attemptId=attempt)
        assert native.countBuild(attempt) == 0
        assert native.countBuild(baseline_build) == 1

        # An already accepted native request can arrive after immediate cleanup.
        native.writeBuild(buildId=attempt, units=late_units)
        assert native.countBuild(attempt) == 1
        assert {row["build_id"] for row in asyncio.run(discover("climate"))} == {baseline_build}
        with sessions.begin() as db:
            failed_publication = db.get(DocumentEnrichmentPublication, attempt)
            assert failed_publication and failed_publication.cleanup_after
            failed_publication.cleanup_after = LexicalCoordination.getDatabaseTime(db)
        assert AdditiveCleanup(sessions).run() == 1
        assert native.countBuild(attempt) == 0

        registry_attempt = f"hot-registry-{suffix}"
        with sessions.begin() as db:
            additivepublication.AdditivePublication.storeBody(db, scope=scope, scopeId="p5-6",
                attemptId=registry_attempt, chunks=[{
                    "chunk_id": "hot", "type": "page", "path": "chapter.pdf/Uncontended",
                    "content": "Uncontended page discovery during a registry transaction",
                    "metadata": {"page_nums": [5, 6]},
                }])
        with ThreadPoolExecutor(max_workers=2) as executor, sessions.begin() as hold:
            held = hold.scalar(select(LexicalNamespaceIndex.id).where(
                LexicalNamespaceIndex.id == native_scope.fingerprint).with_for_update())
            assert held == native_scope.fingerprint
            legacy_started = monotonic()
            legacy = executor.submit(LexicalCoordination(sessions).ensureIndex, scope=native_scope, native=native)
            hot = executor.submit(additivepublication.AdditivePublication.writeIndex, scope=scope, attemptId=registry_attempt)
            hot.result(timeout=4)
            record_property("additive_publication_under_registry_lock_seconds", round(monotonic() - legacy_started, 4))
            assert not legacy.done()
            with pytest.raises(LexicalIndexError, match="Native index admission timed out"):
                legacy.result(timeout=8)
            record_property("legacy_registry_timeout_seconds", round(monotonic() - legacy_started, 4))
            with sessions.begin() as db:
                assert additivepublication.AdditivePublication.complete(db, scope=scope, attemptId=registry_attempt)
            assert {row["build_id"] for row in asyncio.run(discover("uncontended"))} == {registry_attempt}

        completed_attempt = f"completed-{suffix}"
        with sessions.begin() as db:
            additivepublication.AdditivePublication.storeBody(db, scope=scope, scopeId="p1-2",
                attemptId=completed_attempt, chunks=[{
                    "chunk_id": "main", "type": "page", "path": "chapter.pdf/Main",
                    "content": "Climate results from both original pages.",
                    "metadata": {"page_nums": [1, 2], "searchable_page_nums": [1, 2]},
                }])
        additivepublication.AdditivePublication.writeIndex(scope=scope, attemptId=completed_attempt)
        assert additivepublication.AdditivePublication.hasCompleteIndex(scope=scope, attemptId=completed_attempt)
        with sessions.begin() as db:
            assert additivepublication.AdditivePublication.complete(db, scope=scope, attemptId=completed_attempt)
        before_rebuild = asyncio.run(discover("climate"))
        assert {row["build_id"] for row in before_rebuild} == {baseline_build, completed_attempt}
        assert native.deleteBuild(completed_attempt)

        with sessions.begin() as db:
            control = LexicalCoordination.lockControl(db, isExclusive=True)
            control.mode = "gated"
            epoch = control.epoch
        transition = LexicalTransition(sessionFactory=sessions)
        binding = _Binding("private", scope.user_id, scope.namespace, scope.document_id,
            scope.job_result_id, fixture.job_ref, None, None, None, baseline_build)
        transition._restoreEnrichmentRecords(binding, epoch, native)
        with sessions.begin() as db:
            LexicalCoordination.lockControl(db, isExclusive=True, requiresServing=False).mode = "serving"
        after_rebuild = asyncio.run(discover("climate"))
        assert {(row["build_id"], row["source_row_id"], row["content"]) for row in after_rebuild} == {
            (row["build_id"], row["source_row_id"], row["content"]) for row in before_rebuild
        }
        assert native.countBuild(attempt) == 0

        async def archiveDocument() -> None:
            async_engine = create_async_engine(get_contract_database_url())
            try:
                async with AsyncSession(async_engine, expire_on_commit=False) as db:
                    archived = await DocumentService().archive_document(db, user_id=scope.user_id,
                        document_id=scope.document_id)
                    assert archived is not None and archived["status"] == "archived"
            finally:
                await async_engine.dispose()

        asyncio.run(archiveDocument())
        assert not asyncio.run(discover("climate"))
        assert native.countBuild(baseline_build) == 1
        assert native.countBuild(completed_attempt) == 1
        assert asyncio.run(readRetained(first_row_id))
        with sessions.begin() as db:
            document = db.get(Document, scope.document_id)
            assert document and document.index_cleanup_after is not None
            document.index_cleanup_after = LexicalCoordination.getDatabaseTime(db)
        assert AdditiveCleanup(sessions).run() == 1
        assert native.countBuild(baseline_build) == 0
        assert native.countBuild(completed_attempt) == 0
        assert native.countBuild(neighbor_attempt) == 0
        assert {row["document_id"] for row in asyncio.run(discover("induction"))} == {initial_document}
    finally:
        try:
            deleted = httpx.delete(f"{endpoint}/{native.indexName}", timeout=15, trust_env=False)
            assert deleted.status_code in (200, 404)
        finally:
            native.close()
            engine.dispose()
