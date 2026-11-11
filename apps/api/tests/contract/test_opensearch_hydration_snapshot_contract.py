"""A native build resolves its original chunks after progressive replacement."""

import asyncio
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)


def test_build_hydration_keeps_replaced_chunks_and_shares_unchanged_payloads(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())

    from shared.core.config import settings
    from shared.core.exceptions.domain_exceptions import UnavailableException
    from shared.models.database.document import Document, DocumentChunk, DocumentSection
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.models.database.user import User
    from shared.services.retrieval.document_scope import DocumentScope
    from shared.services.retrieval.execution.revision_pins import RetrievalRevisionPins
    from shared.services.retrieval.hydration.build_snapshot import BuildSnapshot
    from shared.services.retrieval.hydration.connected import (
        hydrate_connected_target_rows,
    )
    from shared.services.retrieval.lexical.asyncread import AsyncPublishedLexicalRead
    from shared.services.retrieval.lexical.discovery import discoverOpenSearch
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.hit import LexicalHit
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.unit import LexicalUnit
    from shared.services.retrieval.search.map_unit_discovery import DiscoveryResult

    scope = LexicalScope(ownerId="hydration-user", namespace="hydration")
    otherScope = LexicalScope(ownerId="different-user", namespace=scope.namespace)
    databaseUrl: str = get_contract_database_url()
    engine = create_engine(make_url(databaseUrl).set(drivername="postgresql+psycopg2"))
    documentId: str = "doc_hydration"
    revisionId: str = "revision-hydration"
    jobId: str = "job-hydration"
    oldBuildId: str = "build-coarse"
    newBuildId: str = "build-refined"
    staleBuildId: str = "build-stale"
    sectionId: str = "section-shared"
    common: dict[str, str] = {
        "user_id": scope.ownerId,
        "namespace": scope.namespace,
        "document_id": documentId,
        "job_result_id": revisionId,
    }

    async def loadRows() -> dict[str, list[dict[str, Any]]]:
        asyncEngine = create_async_engine(databaseUrl)
        try:
            async with AsyncSession(asyncEngine) as db:
                sectionKeys: tuple[tuple[str, str], ...] = (
                    (oldBuildId, sectionId),
                    (newBuildId, sectionId),
                )
                chunkKeys: tuple[tuple[str, str], ...] = (
                    (oldBuildId, "old-image"),
                    (newBuildId, "old-image"),
                    (newBuildId, "new-image"),
                    (oldBuildId, "primary"),
                )
                rows = await BuildSnapshot.loadRows(
                    db, scope=scope, sectionKeys=sectionKeys, chunkKeys=chunkKeys
                )
                assert (
                    await BuildSnapshot.loadRows(
                        db,
                        scope=otherScope,
                        sectionKeys=sectionKeys,
                        chunkKeys=chunkKeys,
                    )
                    == []
                )
                reader = AsyncPublishedLexicalRead(
                    userId=scope.ownerId,
                    namespace=scope.namespace,
                    bind=asyncEngine,
                    isClassic=True,
                )
                reader.manifestId = "coarse-manifest"
                coarseHit = LexicalHit(
                    buildId=oldBuildId,
                    score=0.2,
                    unit=LexicalUnit(
                        unitId="coarse-unit",
                        documentId=documentId,
                        revisionId=revisionId,
                        sectionId=sectionId,
                    ),
                )
                nativeHits: list[LexicalHit] = [coarseHit]

                async def searchManifest(
                    **_arguments: object,
                ) -> tuple[LexicalHit, ...]:
                    return tuple(nativeHits)

                async def discover(
                    chunkTypes: set[str] | None = None,
                ) -> DiscoveryResult:
                    return await discoverOpenSearch(
                        db,
                        userId=scope.ownerId,
                        namespace=scope.namespace,
                        query="coarse",
                        topK=3,
                        documentScope=DocumentScope(),
                        excludeDocumentIds=[],
                        excludeSections=[],
                        chunkTypes=chunkTypes,
                        signalPaths=None,
                        filterMode="delete",
                        revisionPins=None,
                        sectionTargets=None,
                    )

                monkeypatch.setattr(
                    reader, "searchManifestAsync", searchManifest, raising=False
                )
                with reader.bind():
                    connected = await hydrate_connected_target_rows(
                        db=db,
                        rows=[row for row in rows if row["chunk_id"] == "primary"],
                        exclude_document_ids=[],
                        exclude_sections=[],
                    )
                    assert {
                        (row["build_id"], row["chunk_id"]) for row in connected
                    } == {
                        (oldBuildId, "old-image"),
                        (newBuildId, "new-image"),
                    }
                    result = await discover({"image"})
                    assert [
                        (row["build_id"], row["chunk_id"], row["content"])
                        for row in result.payload["fused_rows"]
                    ] == [(oldBuildId, "old-image", "Coarse image")]
                    nativeHits[:] = [
                        coarseHit.model_copy(update={"buildId": staleBuildId})
                    ]
                    with pytest.raises(UnavailableException) as failure:
                        await discover()
                    assert (
                        failure.value.details["reason"] == "LEXICAL_BACKEND_UNAVAILABLE"
                    )
                    nativeHits.clear()
                    noMatches = await discover()
                    assert noMatches.payload["fused_rows"] == []
                    assert not noMatches.has_unhydrated_hits
                    reader.manifestId = None
                    reader.pins = RetrievalRevisionPins({documentId: revisionId})
                    monkeypatch.setattr(reader, "searchAsync", searchManifest)
                    reader.builds = {documentId: oldBuildId}
                    nativeHits[:] = [coarseHit]
                    retained = await discover()
                    assert [
                        row["content"] for row in retained.payload["fused_rows"]
                    ] == ["Coarse body"]

                    reader.builds = {documentId: staleBuildId}
                    nativeHits[:] = [
                        coarseHit.model_copy(update={"buildId": staleBuildId})
                    ]
                    stale = await discover()
                    assert stale.payload["fused_rows"] == []
                    assert stale.has_unhydrated_hits

                    await db.execute(
                        text(
                            "UPDATE documents SET current_indexing_build_id = :buildId WHERE document_id = :documentId"
                        ),
                        {"buildId": staleBuildId, "documentId": documentId},
                    )
                    current = await discover()
                    assert [
                        row["content"] for row in current.payload["fused_rows"]
                    ] == ["Refined body"]
                    assert not current.has_unhydrated_hits
                    connected = await hydrate_connected_target_rows(
                        db=db,
                        rows=current.payload["fused_rows"],
                        exclude_document_ids=[],
                        exclude_sections=[],
                    )
                    assert [row["content"] for row in connected] == ["Refined image"]
                    await db.rollback()

                    reader.builds = {documentId: oldBuildId}
                    nativeHits[:] = [coarseHit]
                    await db.execute(
                        text(
                            "UPDATE documents SET status = 'archived' WHERE document_id = :documentId"
                        ),
                        {"documentId": documentId},
                    )
                    archived = await discover()
                    assert archived.payload["fused_rows"] == []
                    assert archived.has_unhydrated_hits
                    await db.rollback()

                    inlineArguments: dict[str, object] = {}

                    async def searchInline(
                        **arguments: object,
                    ) -> tuple[LexicalHit, ...]:
                        inlineArguments.update(arguments)
                        return tuple(nativeHits)

                    monkeypatch.setattr(
                        settings, "KNOWHERE_OPENSEARCH_MANIFEST_LOOKUP", True
                    )
                    monkeypatch.setattr(reader, "searchAsync", searchInline)
                    filtered = await discover({"image"})
                    assert [
                        row["chunk_id"] for row in filtered.payload["fused_rows"]
                    ] == ["old-image"]
                    assert inlineArguments["unitIds"] is None
                    assert inlineArguments["extraFilters"] == (
                        {
                            "bool": {
                                "should": [{"term": {"hasImage": True}}],
                                "minimum_should_match": 1,
                            }
                        },
                    )
                return {
                    buildId: [row for row in rows if row["build_id"] == buildId]
                    for buildId in (oldBuildId, newBuildId)
                }
        finally:
            await asyncEngine.dispose()

    try:
        with Session(engine) as db, db.begin():
            db.add(
                User(
                    id=scope.ownerId,
                    name="Hydration user",
                    email="hydration@example.invalid",
                )
            )
            db.flush()
            db.add(
                Job(
                    job_id=jobId,
                    user_id=scope.ownerId,
                    job_type="document_ingestion",
                    source_type="file",
                    job_metadata={"source_file_name": "coarse.pdf"},
                )
            )
            document = Document(
                document_id=documentId, user_id=scope.ownerId, namespace=scope.namespace
            )
            db.add(document)
            db.flush()
            db.add(
                JobResult(
                    id=revisionId,
                    document_id=documentId,
                    job_id=jobId,
                    delivery_mode="url",
                    document_metadata={"result_raw_prefix": "coarse/raw"},
                )
            )
            db.add(
                LexicalNamespaceIndex(
                    id=scope.fingerprint,
                    ownerId=scope.ownerId,
                    namespace=scope.namespace,
                    corpusKind="private",
                    indexName="hydration-contract",
                    state="open",
                )
            )
            db.flush()
            db.add(
                IndexingAttempt(
                    id="attempt-hydration", jobId=jobId, revisionId=revisionId
                )
            )
            db.flush()
            for buildId in (oldBuildId, newBuildId, staleBuildId):
                db.add(
                    LexicalBuild(
                        id=buildId,
                        attemptId="attempt-hydration",
                        namespaceIndexId=scope.fingerprint,
                        documentId=documentId,
                        revisionId=revisionId,
                        ownerToken="hydration-owner",
                        state="published",
                    )
                )
            section = DocumentSection(
                section_id=sectionId, section_path="Coarse chapter", **common
            )
            db.add(section)
            db.flush()
            db.add_all(
                [
                    DocumentChunk(
                        id="coarse-primary",
                        chunk_id="primary",
                        section_id=sectionId,
                        chunk_type="page",
                        content="Coarse body",
                        source_chunk_path="Chapter/Body",
                        sort_order=1,
                        chunk_metadata={
                            "page_nums": [2],
                            "connect_to": [{"target": "old-image"}],
                        },
                        **common,
                    ),
                    DocumentChunk(
                        id="coarse-image",
                        chunk_id="old-image",
                        chunk_type="image",
                        content="Coarse image",
                        file_path="images/coarse.png",
                        source_chunk_path="Chapter/Image",
                        sort_order=2,
                        **common,
                    ),
                    DocumentChunk(
                        id="coarse-stable",
                        chunk_id="stable",
                        content="Shared payload",
                        source_chunk_path="Appendix",
                        sort_order=3,
                        **common,
                    ),
                ]
            )
            document.current_job_result_id = revisionId
            document.current_indexing_build_id = oldBuildId
            BuildSnapshot.capture(db, scope=scope, buildId=oldBuildId)

        with Session(engine) as db, db.begin():
            db.execute(
                text("DELETE FROM document_chunks WHERE document_id = :documentId"),
                {"documentId": documentId},
            )
            db.execute(
                text(
                    "UPDATE document_sections SET section_path = 'Refined chapter' WHERE section_id = :sectionId"
                ),
                {"sectionId": sectionId},
            )
            job = db.get(Job, jobId)
            result = db.get(JobResult, revisionId)
            document = db.get(Document, documentId)
            assert job is not None and result is not None and document is not None
            job.job_metadata = {"source_file_name": "refined.pdf"}
            result.document_metadata = {"result_raw_prefix": "refined/raw"}
            db.add_all(
                [
                    DocumentChunk(
                        id="refined-primary",
                        chunk_id="primary",
                        section_id=sectionId,
                        chunk_type="page",
                        content="Refined body",
                        source_chunk_path="Chapter/Body",
                        sort_order=1,
                        chunk_metadata={
                            "page_nums": [7],
                            "connect_to": [{"target": "new-image"}],
                        },
                        **common,
                    ),
                    DocumentChunk(
                        id="refined-image",
                        chunk_id="new-image",
                        chunk_type="image",
                        content="Refined image",
                        file_path="images/refined.png",
                        source_chunk_path="Chapter/Image",
                        sort_order=2,
                        **common,
                    ),
                    DocumentChunk(
                        id="refined-stable",
                        chunk_id="stable",
                        content="Shared payload",
                        source_chunk_path="Appendix",
                        sort_order=3,
                        **common,
                    ),
                ]
            )
            document.current_indexing_build_id = newBuildId
            BuildSnapshot.capture(db, scope=scope, buildId=newBuildId)
            BuildSnapshot.capture(db, scope=scope, buildId=oldBuildId)
            with pytest.raises(LexicalIndexError, match="current lexical build"):
                BuildSnapshot.capture(db, scope=scope, buildId=staleBuildId)

        rowsByBuild = asyncio.run(loadRows())
        oldRows: list[dict[str, Any]] = rowsByBuild[oldBuildId]
        newRows: list[dict[str, Any]] = rowsByBuild[newBuildId]
        assert [row["chunk_id"] for row in oldRows] == ["primary", "old-image"]
        assert [row["chunk_id"] for row in newRows] == ["primary", "new-image"]
        assert oldRows[0]["content"] == "Coarse body"
        assert newRows[0]["content"] == "Refined body"
        assert oldRows[0]["section_path"] == "Coarse chapter"
        assert newRows[0]["section_path"] == "Refined chapter"
        assert oldRows[0]["chunk_metadata"] == {
            "page_nums": [2],
            "connect_to": [{"target": "old-image"}],
        }
        assert newRows[0]["chunk_metadata"] == {
            "page_nums": [7],
            "connect_to": [{"target": "new-image"}],
        }
        assert oldRows[0]["source_file_name"] == "coarse.pdf"
        assert newRows[0]["source_file_name"] == "refined.pdf"
        assert oldRows[0]["result_raw_prefix"] == "coarse/raw"
        assert newRows[0]["result_raw_prefix"] == "refined/raw"
        assert oldRows[1]["file_path"] == "images/coarse.png"
        assert newRows[1]["file_path"] == "images/refined.png"
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM lexical_hydration_rows"))
                == 6
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM lexical_hydration_payloads")
                )
                == 5
            )
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_assembly_keeps_identical_connected_chunk_ids_from_other_builds_separate() -> (
    None
):
    from shared.services.retrieval.hydration.result_assembly import (
        assemble_retrieval_results,
    )

    rows: list[dict[str, Any]] = [
        {
            "build_id": "coarse",
            "document_id": "first",
            "chunk_id": "parent",
            "chunk_type": "text",
            "content": "Parent",
            "chunk_metadata": {"connect_to": [{"target": "shared"}]},
        },
        {
            "build_id": "refined",
            "document_id": "second",
            "chunk_id": "shared",
            "chunk_type": "table",
            "content": "Second table",
        },
    ]
    assembled = await assemble_retrieval_results(
        rows=rows,
        exclude_document_ids=[],
        exclude_sections=[],
        allowed_chunk_types={"table"},
    )
    assert [row["chunk_id"] for row in assembled] == ["shared"]
