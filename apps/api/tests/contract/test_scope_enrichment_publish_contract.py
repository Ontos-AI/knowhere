"""Progressive publication: scope rows, queueing, read promotion, and any-stage tools."""

from __future__ import annotations

import asyncio

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shared.core.state_machine.transition_outcome import JobTransitionOutcome
from shared.models.database.document import (
    Document,
    DocumentChunk,
    DocumentMapUnitIndex,
    DocumentScopeEnrichment,
    DocumentSection,
)
from shared.services.jobs.lifecycle.scope_enrichments import (
    BUILD_STRUCTURE_TASK_NAME,
    ENRICH_SCOPE_TASK_NAME,
    HIGH_ENRICHMENT_QUEUE,
    HIGH_ENRICHMENT_TASK_PRIORITY,
    NORMAL_ENRICHMENT_QUEUE,
    write_coarse_scope_enrichments,
)
from shared.services.jobs.lifecycle.success_finalizer import (
    JobSuccessFinalization,
    SyncJobSuccessFinalizer,
)
from shared.services.retrieval.agent_tools.registry import ToolContext
from shared.services.retrieval.agent_tools.tools.assets import assets
from shared.services.retrieval.agent_tools.tools.grep import grep
from shared.services.retrieval.agent_tools.tools.list_documents import list_documents
from shared.services.retrieval.agent_tools.tools.node_filter import node_filter
from shared.services.retrieval.agent_tools.tools.outline import outline
from shared.services.retrieval.agent_tools.tools.query_table import query_table
from shared.services.retrieval.agent_tools.tools.read import read
from shared.services.retrieval.agent_tools.tools.recall import recall
from shared.services.retrieval.hydration.result_assembly import (
    assemble_retrieval_results,
)
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.search.map_unit_discovery import map_unit_discovery
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


class _DoneStateMachine:
    def mark_completed_outcome(
        self,
        db: object,
        job_id: str,
        result_metadata: object = None,
        operator_id: object = None,
    ) -> JobTransitionOutcome:
        del db, result_metadata, operator_id
        return JobTransitionOutcome.transitioned(
            job_id=job_id,
            from_state="running",
            to_state="done",
            attempts=1,
        )


def _scope(label: str) -> PublicationScope:
    return PublicationScope(
        user_ref=f"scope-enrich-{label}-user",
        namespace_ref=f"scope-enrich-{label}",
        source_file_name=f"scope-enrich-{label}.pdf",
        job_ref=f"scope-enrich-{label}-job",
        revision_ref=f"scope-enrich-{label}-result",
    )


def _page_chunk(
    *,
    chunk_id: str,
    path: str,
    scope_id: str,
    page_nums: list[int],
) -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "type": "page",
        "content": f"raw {scope_id}",
        "path": path,
        "metadata": {
            "scope_id": scope_id,
            "enrichment_state": "coarse",
            "page_nums": page_nums,
        },
    }


def _set_job_progressive(
    session: Session,
    *,
    job_id: str,
    ingestion_mode: str = "progressive",
    enrichment_status: str = "coarse",
) -> None:
    from shared.models.database.job import Job

    job = session.execute(select(Job).where(Job.job_id == job_id)).scalar_one()
    job.job_metadata = {
        **dict(job.job_metadata or {}),
        "parse_track": "page_memory",
        "ingestion_mode": ingestion_mode,
        "enrichment_status": enrichment_status,
    }
    session.flush()


def _finalize(
    session: Session,
    *,
    job_id: str,
    chunks: list[dict[str, object]],
) -> JobSuccessFinalization:
    return SyncJobSuccessFinalizer(state_machine=_DoneStateMachine()).finalize(
        session,
        job_id=job_id,
        result_s3_key="results/scope-enrich/result.zip",
        checksum="checksum",
        zip_size=1,
        chunks=chunks,
        stored_count=len(chunks),
        delivery_mode="url",
        section_summaries=None,
    )


def _write_scope_rows(session: Session, *, scope: PublicationScope) -> None:
    document_id = session.execute(
        select(Document.document_id).where(
            Document.current_job_result_id == scope.revision_ref
        )
    ).scalar_one()
    assert write_coarse_scope_enrichments(
        session,
        document_id=document_id,
        job_result_id=scope.revision_ref,
    )


def test_write_coarse_scope_enrichments_inserts_one_row_per_published_scope(
    monkeypatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    scope = _scope("page")
    chunks = [
        _page_chunk(
            chunk_id="coarse-a1",
            path=f"{scope.source_file_name}/A",
            scope_id="p1-2",
            page_nums=[1, 2],
        ),
        _page_chunk(
            chunk_id="coarse-a2",
            path=f"{scope.source_file_name}/A2",
            scope_id="p1-2",
            page_nums=[1, 2],
        ),
        _page_chunk(
            chunk_id="coarse-b",
            path=f"{scope.source_file_name}/B",
            scope_id="p3-4",
            page_nums=[3, 4],
        ),
    ]
    try:
        with Session(engine) as session:
            seed_publication_fixture(session, scope=scope)
            _set_job_progressive(session, job_id=scope.job_ref)
            session.commit()
            _finalize(session, job_id=scope.job_ref, chunks=chunks)
            _write_scope_rows(session, scope=scope)
            session.commit()

        with Session(engine) as session:
            rows = (
                session.execute(
                    select(DocumentScopeEnrichment)
                    .where(DocumentScopeEnrichment.job_result_id == scope.revision_ref)
                    .order_by(DocumentScopeEnrichment.scope_id)
                )
                .scalars()
                .all()
            )
            assert [
                (row.scope_id, row.page_start, row.page_end, row.state) for row in rows
            ] == [
                ("p1-2", 1, 2, "coarse"),
                ("p3-4", 3, 4, "coarse"),
            ]
            assert {row.document_id for row in rows} == {rows[0].document_id}
            assert all(row.priority is None for row in rows)
            assert all(row.attempts == 0 for row in rows)
            assert all(row.refined_at is None for row in rows)

        with Session(engine) as session:
            _write_scope_rows(session, scope=scope)
            session.commit()

        with Session(engine) as session:
            replay_count = len(
                session.execute(
                    select(DocumentScopeEnrichment).where(
                        DocumentScopeEnrichment.job_result_id == scope.revision_ref
                    )
                )
                .scalars()
                .all()
            )
            assert replay_count == 2
    finally:
        engine.dispose()


def test_finalize_job_success_skips_enrichments_without_scope_id(
    monkeypatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    scope = _scope("text")
    try:
        with Session(engine) as session:
            seed_publication_fixture(session, scope=scope)
            session.commit()
            _finalize(
                session,
                job_id=scope.job_ref,
                chunks=[
                    {
                        "chunk_id": "text-body",
                        "type": "text",
                        "content": "plain text",
                        "path": f"{scope.source_file_name}/Root/Body",
                        "metadata": {},
                    }
                ],
            )
            session.commit()

        with Session(engine) as session:
            rows = (
                session.execute(
                    select(DocumentScopeEnrichment).where(
                        DocumentScopeEnrichment.job_result_id == scope.revision_ref
                    )
                )
                .scalars()
                .all()
            )
            assert rows == []
    finally:
        engine.dispose()


def test_result_assembly_branches_explicitly_on_page_enrichment_state() -> None:
    rows = [
        {
            "chunk_id": "page-text",
            "document_id": "doc-1",
            "chunk_type": "page",
            "content": "raw m0 page text",
            "chunk_metadata": {
                "scope_id": "p1-1",
                "enrichment_state": "text",
                "summary": "",
                "page_nums": [1],
            },
        },
        {
            "chunk_id": "page-coarse",
            "document_id": "doc-1",
            "chunk_type": "page",
            "content": "raw page one text",
            "chunk_metadata": {
                "scope_id": "p1-2",
                "enrichment_state": "coarse",
                "summary": "",
                "page_nums": [1, 2],
            },
        },
        {
            "chunk_id": "page-queued",
            "document_id": "doc-1",
            "chunk_type": "page",
            "content": "raw page three text",
            "chunk_metadata": {
                "scope_id": "p3-4",
                "enrichment_state": "queued",
                "summary": "ignored while queued",
                "page_nums": [3, 4],
            },
        },
        {
            "chunk_id": "page-refined-with-summary",
            "document_id": "doc-1",
            "chunk_type": "page",
            "content": "raw page five text",
            "chunk_metadata": {
                "scope_id": "p5-6",
                "enrichment_state": "refined",
                "summary": "refined vlm summary",
                "page_nums": [5, 6],
            },
        },
        {
            "chunk_id": "page-refined-empty-summary",
            "document_id": "doc-1",
            "chunk_type": "page",
            "content": "raw text must not leak on refined",
            "chunk_metadata": {
                "scope_id": "p7-8",
                "enrichment_state": "refined",
                "summary": "",
                "page_nums": [7, 8],
            },
        },
    ]

    assembled = asyncio.run(
        assemble_retrieval_results(
            rows=rows,
            exclude_document_ids=[],
            exclude_sections=[],
        )
    )
    by_id = {row["chunk_id"]: row for row in assembled}

    assert by_id["page-text"]["content"] == "raw m0 page text"
    assert by_id["page-text"]["content_source"] == "raw"
    assert by_id["page-text"]["page_nums"] == [1]
    assert by_id["page-text"]["composed"] == [
        {"type": "text", "text": "raw m0 page text"}
    ]

    assert by_id["page-coarse"]["content"] == "raw page one text"
    assert by_id["page-coarse"]["content_source"] == "raw"
    assert by_id["page-coarse"]["page_nums"] == [1, 2]
    assert by_id["page-coarse"]["composed"] == [
        {"type": "text", "text": "raw page one text"}
    ]

    assert by_id["page-queued"]["content"] == "raw page three text"
    assert by_id["page-queued"]["content_source"] == "raw"

    assert by_id["page-refined-with-summary"]["content"] == "refined vlm summary"
    assert by_id["page-refined-with-summary"]["content_source"] == "summary"

    # Explicit branch: refined never falls back to raw content when summary is empty.
    assert by_id["page-refined-empty-summary"]["content"] == ""
    assert by_id["page-refined-empty-summary"]["content_source"] == "summary"


def test_replace_document_scope_content_isolates_scope_reuses_ancestor_rebuilds_map_units_and_bumps_cache(
    monkeypatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    from shared.core.database import get_db_context
    from shared.services.redis import redis_sync_service
    from shared.services.retrieval.cache_service import (
        get_retrieval_namespace_cache_version as get_cache_version,
    )
    from shared.services.retrieval.publication_scope_content import (
        replace_document_scope_content as replace_scope_content,
    )
    from shared.testing.contract_runtime import (
        CONTRACT_REDIS_DATABASE,
        CONTRACT_REDIS_HOST,
        CONTRACT_REDIS_PORT,
        ContractFakeSyncRedis,
    )

    monkeypatch.setattr(
        redis_sync_service,
        "SyncRedisClient",
        lambda **_kw: ContractFakeSyncRedis.from_url(
            f"redis://{CONTRACT_REDIS_HOST}:{CONTRACT_REDIS_PORT}/{CONTRACT_REDIS_DATABASE}"
        ),
    )
    redis_sync_service.SyncRedisServiceFactory.reset()

    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    scope = _scope("partial")
    coarse_chunks = [
        _page_chunk(
            chunk_id="coarse-p1-2",
            path=f"{scope.source_file_name}/SharedParent/OldLeafA",
            scope_id="p1-2",
            page_nums=[1, 2],
        )
        | {"content": "alpha coarse unique token"},
        _page_chunk(
            chunk_id="coarse-p3-4",
            path=f"{scope.source_file_name}/SharedParent/SiblingLeafB",
            scope_id="p3-4",
            page_nums=[3, 4],
        )
        | {"content": "bravo sibling untouched token"},
        _page_chunk(
            chunk_id="coarse-p5-6",
            path=f"{scope.source_file_name}/OtherChapter/LeafC",
            scope_id="p5-6",
            page_nums=[5, 6],
        )
        | {"content": "charlie third scope token"},
    ]
    try:
        with Session(engine) as session:
            seed_publication_fixture(session, scope=scope)
            _set_job_progressive(session, job_id=scope.job_ref)
            session.commit()
            _finalize(session, job_id=scope.job_ref, chunks=coarse_chunks)
            session.commit()

        # Coarse retrieval works across the full document right after Phase A.
        async def _discover_and_assemble(query_text: str) -> list[dict[str, object]]:
            async with get_db_context() as async_db:
                discovered = await map_unit_discovery(
                    async_db,
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    query=query_text,
                    top_k=5,
                    exclude_document_ids=[],
                    exclude_sections=[],
                )
                fused = list(discovered.payload.get("fused_rows") or [])
                return await assemble_retrieval_results(
                    db=async_db,
                    rows=fused,
                    exclude_document_ids=[],
                    exclude_sections=[],
                )

        coarse_hits = asyncio.run(_discover_and_assemble("alpha coarse unique"))
        assert any(
            row["chunk_id"] == "coarse-p1-2"
            and row["content"] == "alpha coarse unique token"
            and row["content_source"] == "raw"
            for row in coarse_hits
        )

        version_before = asyncio.run(
            get_cache_version(
                user_id=scope.user_ref,
                namespace=scope.namespace_ref,
            )
        )

        with Session(engine) as session:
            doc = session.execute(
                select(Document).where(
                    Document.current_job_result_id == scope.revision_ref
                )
            ).scalar_one()
            sections_before = {
                sec.section_path: sec.section_id
                for sec in session.scalars(
                    select(DocumentSection).where(
                        DocumentSection.job_result_id == scope.revision_ref
                    )
                ).all()
            }
            parent_sec_id_before = sections_before["SharedParent"]
            sibling_sec_id_before = sections_before["SharedParent / SiblingLeafB"]
            sibling_chunk_before = session.execute(
                select(DocumentChunk).where(
                    DocumentChunk.job_result_id == scope.revision_ref,
                    DocumentChunk.chunk_id == "coarse-p3-4",
                )
            ).scalar_one()
            sibling_chunk_pk_before = sibling_chunk_before.id

            replace_scope_content(
                session,
                scope=DocumentPublicationScope(
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    document_id=doc.document_id,
                    job_result_id=scope.revision_ref,
                    source_file_name=scope.source_file_name,
                ),
                scope_id="p1-2",
                chunks=[
                    {
                        "chunk_id": "refined-a1",
                        "type": "page",
                        "content": "delta refined first leaf raw body",
                        "path": f"{scope.source_file_name}/SharedParent/RefinedA1",
                        "metadata": {
                            "scope_id": "p1-2",
                            "summary": "delta refined summary a1",
                            "page_nums": [1],
                        },
                        "order": 0,
                    },
                    {
                        "chunk_id": "refined-a2",
                        "type": "page",
                        "content": "epsilon refined second leaf raw body",
                        "path": f"{scope.source_file_name}/SharedParent/RefinedA2",
                        "metadata": {
                            "scope_id": "p1-2",
                            "summary": "epsilon refined summary a2",
                            "page_nums": [2],
                        },
                        "order": 1,
                    },
                ],
                section_summaries={
                    "SharedParent": "This section covers: RefinedA1, RefinedA2",
                    "SharedParent / RefinedA1": "delta refined summary a1",
                    "SharedParent / RefinedA2": "epsilon refined summary a2",
                },
            )
            session.commit()

        version_after = asyncio.run(
            get_cache_version(
                user_id=scope.user_ref,
                namespace=scope.namespace_ref,
            )
        )
        assert version_after == version_before + 1

        with Session(engine) as session:
            sections_after = {
                sec.section_path: sec
                for sec in session.scalars(
                    select(DocumentSection).where(
                        DocumentSection.job_result_id == scope.revision_ref
                    )
                ).all()
            }
            assert "SharedParent / OldLeafA" not in sections_after
            assert sections_after["SharedParent"].section_id == parent_sec_id_before
            assert (
                sections_after["SharedParent / SiblingLeafB"].section_id
                == sibling_sec_id_before
            )
            assert (
                sections_after["SharedParent / RefinedA1"].parent_section_id
                == parent_sec_id_before
            )
            assert (
                sections_after["SharedParent / RefinedA2"].parent_section_id
                == parent_sec_id_before
            )

            chunks_after = list(
                session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.job_result_id == scope.revision_ref)
                    .order_by(DocumentChunk.sort_order)
                ).all()
            )
            assert [c.chunk_id for c in chunks_after] == [
                "refined-a1",
                "refined-a2",
                "coarse-p3-4",
                "coarse-p5-6",
            ]
            assert chunks_after[2].id == sibling_chunk_pk_before
            assert chunks_after[0].chunk_metadata["enrichment_state"] == "refined"
            assert chunks_after[1].chunk_metadata["enrichment_state"] == "refined"
            assert chunks_after[2].chunk_metadata["enrichment_state"] == "coarse"
            assert chunks_after[3].chunk_metadata["enrichment_state"] == "coarse"

            map_index = session.execute(
                select(DocumentMapUnitIndex).where(
                    DocumentMapUnitIndex.job_result_id == scope.revision_ref
                )
            ).scalar_one()
            assert map_index.unit_count == 4

        # Refined scope now serves summary, while unrefined sibling scope still serves raw.
        refined_hits = asyncio.run(_discover_and_assemble("delta refined first"))
        assert any(
            row["chunk_id"] == "refined-a1"
            and row["content"] == "delta refined summary a1"
            and row["content_source"] == "summary"
            for row in refined_hits
        )
        sibling_hits = asyncio.run(_discover_and_assemble("bravo sibling untouched"))
        assert any(
            row["chunk_id"] == "coarse-p3-4"
            and row["content"] == "bravo sibling untouched token"
            and row["content_source"] == "raw"
            for row in sibling_hits
        )
    finally:
        engine.dispose()


def test_read_promotes_coarse_and_queued_scopes_with_dedup_including_descendants(
    monkeypatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    from shared.core import celery_app as celery_app_mod
    from shared.core.database import get_db_context

    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    scope = _scope("read-promote")
    sent_tasks: list[tuple[str, list[object], str | None, int | None]] = []

    class _FakeCelery:
        def send_task(
            self,
            name: str,
            args: list[object] | None = None,
            queue: str | None = None,
            priority: int | None = None,
            **kwargs: object,
        ) -> None:
            del kwargs
            sent_tasks.append((name, list(args or []), queue, priority))

    monkeypatch.setattr(celery_app_mod, "get_celery_app", lambda: _FakeCelery())

    chunks = [
        _page_chunk(
            chunk_id="chunk-p1-2",
            path=f"{scope.source_file_name}/Chapter1/SecA",
            scope_id="p1-2",
            page_nums=[1, 2],
        ),
        _page_chunk(
            chunk_id="chunk-p3-4",
            path=f"{scope.source_file_name}/Chapter1/SecB",
            scope_id="p3-4",
            page_nums=[3, 4],
        ),
        {
            "chunk_id": "chunk-p5-6",
            "type": "page",
            "content": "already refined",
            "path": f"{scope.source_file_name}/Chapter1/SecC",
            "metadata": {
                "scope_id": "p5-6",
                "enrichment_state": "refined",
                "summary": "refined sec c",
                "page_nums": [5, 6],
            },
        },
    ]
    try:
        with Session(engine) as session:
            seed_publication_fixture(session, scope=scope)
            _set_job_progressive(session, job_id=scope.job_ref)
            session.commit()
            _finalize(session, job_id=scope.job_ref, chunks=chunks)
            _write_scope_rows(session, scope=scope)
            # Mark p3-4 as low-queued and p5-6 as already refined before read.
            for row in session.scalars(
                select(DocumentScopeEnrichment).where(
                    DocumentScopeEnrichment.job_result_id == scope.revision_ref
                )
            ).all():
                if row.scope_id == "p3-4":
                    row.state = "queued"
                    row.priority = "low"
                elif row.scope_id == "p5-6":
                    row.state = "refined"
            doc_id = session.execute(
                select(Document.document_id).where(
                    Document.current_job_result_id == scope.revision_ref
                )
            ).scalar_one()
            session.commit()

        async def _call_read() -> None:
            async with get_db_context() as async_db:
                ctx = ToolContext(
                    db=async_db,
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    db_factory=get_db_context,
                )
                result = await read(
                    ctx,
                    {
                        "refs": [
                            {
                                "document_id": doc_id,
                                "section_path": "Chapter1",
                            }
                        ],
                        "mode": "descendants",
                    },
                )
                assert result.error is None

        asyncio.run(_call_read())

        assert sorted(sent_tasks, key=lambda item: str(item[1][1])) == [
            (
                ENRICH_SCOPE_TASK_NAME,
                [scope.revision_ref, "p1-2"],
                HIGH_ENRICHMENT_QUEUE,
                HIGH_ENRICHMENT_TASK_PRIORITY,
            ),
            (
                ENRICH_SCOPE_TASK_NAME,
                [scope.revision_ref, "p3-4"],
                HIGH_ENRICHMENT_QUEUE,
                HIGH_ENRICHMENT_TASK_PRIORITY,
            ),
        ]

        # Reading the same coarse/queued scopes a second time must not re-enqueue.
        asyncio.run(_call_read())
        assert len(sent_tasks) == 2

        with Session(engine) as session:
            rows = (
                session.execute(
                    select(DocumentScopeEnrichment)
                    .where(
                        DocumentScopeEnrichment.job_result_id == scope.revision_ref
                    )
                    .order_by(DocumentScopeEnrichment.scope_id)
                )
                .scalars()
                .all()
            )
            assert [(r.scope_id, r.state, r.priority) for r in rows] == [
                ("p1-2", "queued", "high"),
                ("p3-4", "queued", "high"),
                ("p5-6", "refined", None),
            ]
            assert rows[0].last_hit_at is not None
            assert rows[1].last_hit_at is not None
    finally:
        engine.dispose()


def test_full_mode_does_not_write_scope_rows_or_enqueue_tasks(
    monkeypatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    scope = _scope("full-mode")
    chunks = [
        {
            "chunk_id": "full-p1",
            "type": "page",
            "content": "full refined body",
            "path": f"{scope.source_file_name}/A",
            "metadata": {
                "scope_id": "p1-2",
                "enrichment_state": "coarse",
                "summary": "full summary",
                "page_nums": [1, 2],
            },
        }
    ]
    try:
        with Session(engine) as session:
            seed_publication_fixture(session, scope=scope)
            _set_job_progressive(
                session,
                job_id=scope.job_ref,
                ingestion_mode="full",
                enrichment_status="",
            )
            session.commit()
            finalization = _finalize(
                session, job_id=scope.job_ref, chunks=chunks
            )
            session.commit()

        assert finalization.post_commit_effects.structure_job_result_ids == ()
        with Session(engine) as session:
            rows = (
                session.execute(
                    select(DocumentScopeEnrichment).where(
                        DocumentScopeEnrichment.job_result_id == scope.revision_ref
                    )
                )
                .scalars()
                .all()
            )
            assert rows == []
    finally:
        engine.dispose()


def test_m0_text_publish_enqueues_structure_and_supports_any_stage_explore_and_ancestor_summary_isolation(
    monkeypatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    from shared.core import celery_app as celery_app_mod
    from shared.core.database import get_db_context
    from shared.services.jobs.lifecycle.post_commit_effects import (
        SyncJobPostCommitEffectRunner as PostCommitRunner,
    )
    from shared.services.jobs.lifecycle.scope_enrichments import (
        write_coarse_scope_enrichments,
    )
    from shared.services.retrieval.publication_scope_content import (
        replace_document_scope_content as replace_scope_content,
    )

    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    scope = _scope("m0-flow")
    sent_tasks: list[tuple[str, list[object], str | None, int | None]] = []

    class _FakeCelery:
        def send_task(
            self,
            name: str,
            args: list[object] | None = None,
            queue: str | None = None,
            priority: int | None = None,
            **kwargs: object,
        ) -> None:
            del kwargs
            sent_tasks.append((name, list(args or []), queue, priority))

    monkeypatch.setattr(celery_app_mod, "get_celery_app", lambda: _FakeCelery())

    m0_chunks = [
        {
            "chunk_id": "m0-p1",
            "type": "page",
            "content": "zeta baseline m0 page one unique text",
            "path": f"{scope.source_file_name}/p1",
            "metadata": {
                "scope_id": "p1-1",
                "enrichment_state": "text",
                "page_nums": [1],
            },
        },
        {
            "chunk_id": "m0-p2",
            "type": "page",
            "content": "theta baseline m0 page two unique text",
            "path": f"{scope.source_file_name}/p2",
            "metadata": {
                "scope_id": "p2-2",
                "enrichment_state": "text",
                "page_nums": [2],
            },
        },
        {
            "chunk_id": "m0-p3",
            "type": "page",
            "content": "kappa baseline m0 page three unique text",
            "path": f"{scope.source_file_name}/p3",
            "metadata": {
                "scope_id": "p3-3",
                "enrichment_state": "text",
                "page_nums": [3],
            },
        },
    ]

    try:
        with Session(engine) as session:
            seed_publication_fixture(session, scope=scope)
            _set_job_progressive(
                session,
                job_id=scope.job_ref,
                ingestion_mode="progressive",
                enrichment_status="text",
            )
            session.commit()
            finalization = _finalize(
                session, job_id=scope.job_ref, chunks=m0_chunks
            )
            doc_id = session.execute(
                select(Document.document_id).where(
                    Document.current_job_result_id == scope.revision_ref
                )
            ).scalar_one()
            session.commit()

        assert finalization.post_commit_effects.structure_job_result_ids == (
            scope.revision_ref,
        )
        PostCommitRunner().run(finalization.post_commit_effects)
        assert sent_tasks == [
            (
                BUILD_STRUCTURE_TASK_NAME,
                [scope.revision_ref],
                NORMAL_ENRICHMENT_QUEUE,
                None,
            )
        ]

        # Stage M0 (state=text): recall/grep/read work; outline/node_filter/assets/query_table return informative notes.
        async def _verify_m0_tools() -> None:
            async with get_db_context() as async_db:
                ctx = ToolContext(
                    db=async_db,
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    db_factory=get_db_context,
                )
                recall_res = await recall(ctx, {"query": "zeta baseline m0"})
                assert recall_res.error is None
                assert "candidates=1" not in recall_res.text or "zeta" in recall_res.text
                assert len(recall_res.payload["rows"]) >= 1

                grep_res = await grep(ctx, {"pattern": "zeta baseline"})
                assert grep_res.error is None
                assert "zeta baseline" in grep_res.text

                read_res = await read(
                    ctx,
                    {"refs": [{"document_id": doc_id, "chunk_id": "m0-p1"}]},
                )
                assert read_res.error is None
                assert "zeta baseline m0 page one unique text" in read_res.text

                outline_res = await outline(
                    ctx, {"scope": [{"document_id": doc_id}]}
                )
                assert outline_res.error is None
                assert "still being structured (state=text)" in outline_res.text

                nf_res = await node_filter(
                    ctx,
                    {
                        "scope": [{"document_id": doc_id}],
                        "predicates": [{"field": "path", "terms": ["Root"]}],
                    },
                )
                assert nf_res.error is None
                assert "still being structured (state=text)" in nf_res.text

                assets_res = await assets(
                    ctx, {"scope": [{"document_id": doc_id}]}
                )
                assert assets_res.error is None
                assert "still being structured (state=text)" in assets_res.text

                qt_res = await query_table(
                    ctx,
                    {
                        "document_id": doc_id,
                        "chunk_id": "nonexistent_tbl",
                        "sql": "SELECT * FROM t",
                    },
                )
                assert qt_res.error is not None
                assert "still being structured (state=text)" in qt_res.error

                ld_res = await list_documents(ctx, {})
                assert ld_res.error is None
                assert "status=text" in ld_res.text
                assert ld_res.payload["documents"][0]["status"] == "text"

        asyncio.run(_verify_m0_tools())
        # Reading an M0 page must NOT enqueue scope tasks (no acceleration channel).
        assert len(sent_tasks) == 1

        # Stage 1 (coarse): whole-doc restructure publish + write scope rows.
        from shared.models.database.job_result import JobResult

        with Session(engine) as session:
            _set_job_progressive(
                session,
                job_id=scope.job_ref,
                ingestion_mode="progressive",
                enrichment_status="coarse",
            )
            jr = session.execute(
                select(JobResult).where(JobResult.id == scope.revision_ref)
            ).scalar_one()
            jr.document_metadata = {
                **dict(jr.document_metadata or {}),
                "enrichment_status": "coarse",
            }
            pub_scope = DocumentPublicationScope(
                user_id=scope.user_ref,
                namespace=scope.namespace_ref,
                document_id=doc_id,
                job_result_id=scope.revision_ref,
                source_file_name=scope.source_file_name,
            )
            replace_scope_content(
                session,
                scope=pub_scope,
                scope_id="*",
                chunks=[
                    _page_chunk(
                        chunk_id="coarse-s1",
                        path=f"{scope.source_file_name}/Chapter1/SecA",
                        scope_id="p1-1",
                        page_nums=[1],
                    ),
                    _page_chunk(
                        chunk_id="coarse-s2",
                        path=f"{scope.source_file_name}/Chapter1/SecB",
                        scope_id="p2-2",
                        page_nums=[2],
                    ),
                ],
                section_summaries={
                    "Chapter1": "Original full Chapter1 summary covering SecA and SecB",
                    "Chapter1 / SecA": "SecA coarse summary",
                    "Chapter1 / SecB": "SecB coarse summary",
                },
            )
            write_coarse_scope_enrichments(
                session,
                document_id=doc_id,
                job_result_id=scope.revision_ref,
            )
            session.commit()

        sent_tasks.clear()

        async def _verify_coarse_tools() -> None:
            async with get_db_context() as async_db:
                ctx = ToolContext(
                    db=async_db,
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    db_factory=get_db_context,
                )
                outline_res = await outline(
                    ctx, {"scope": [{"document_id": doc_id}]}
                )
                assert outline_res.error is None
                assert "Chapter1 / SecA" in outline_res.text

                assets_res = await assets(
                    ctx,
                    {
                        "scope": [
                            {
                                "document_id": doc_id,
                                "section_path": "Chapter1 / SecA",
                            }
                        ]
                    },
                )
                assert assets_res.error is None
                assert "not extracted yet (state=coarse)" in assets_res.text

                ld_res = await list_documents(ctx, {})
                assert ld_res.payload["documents"][0]["status"] == "coarse"

        asyncio.run(_verify_coarse_tools())
        # Calling assets on a coarse scope promotes that scope to high priority!
        assert sent_tasks == [
            (
                ENRICH_SCOPE_TASK_NAME,
                [scope.revision_ref, "p1-1"],
                HIGH_ENRICHMENT_QUEUE,
                HIGH_ENRICHMENT_TASK_PRIORITY,
            )
        ]

        # Stage 2 (fine) on p1-1: verify shared ancestor 'Chapter1' summary is NOT overwritten.
        with Session(engine) as session:
            replace_scope_content(
                session,
                scope=DocumentPublicationScope(
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    document_id=doc_id,
                    job_result_id=scope.revision_ref,
                    source_file_name=scope.source_file_name,
                ),
                scope_id="p1-1",
                chunks=[
                    {
                        "chunk_id": "fine-s1",
                        "type": "page",
                        "content": "refined sec a",
                        "path": f"{scope.source_file_name}/Chapter1/SecA",
                        "metadata": {
                            "scope_id": "p1-1",
                            "summary": "SecA fine summary",
                            "page_nums": [1],
                        },
                    }
                ],
                section_summaries={
                    "Chapter1": "Partial overwritten summary mentioning only SecA",
                    "Chapter1 / SecA": "SecA fine summary",
                },
            )
            session.commit()
            ch1 = session.execute(
                select(DocumentSection).where(
                    DocumentSection.job_result_id == scope.revision_ref,
                    DocumentSection.section_path == "Chapter1",
                )
            ).scalar_one()
            assert (
                ch1.summary
                == "Original full Chapter1 summary covering SecA and SecB"
            )
    finally:
        engine.dispose()


def test_scanned_visual_m0_retrieval_evidence_and_cross_stage_embedding_reuse(
    monkeypatch,
    postgresql_proc: PostgreSQLProcess,
    tmp_path,
) -> None:
    import base64
    from pathlib import Path

    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    scope = _scope("vism0")

    from shared.core.config import settings
    from shared.core.database import get_db_context
    from shared.models.database.document import DocumentPageEmbedding
    from shared.services.ai.embedding import DEFAULT_EMBEDDING_DIM, multimodal_client
    from shared.services.retrieval.agent_tools.tools.grep import grep as fresh_grep
    from shared.services.retrieval.agent_tools.tools.read import read as fresh_read
    from shared.services.retrieval.agent_tools.tools.recall import recall as fresh_recall
    from shared.services.retrieval.execution.route_types import RetrievalRouteContext
    from shared.services.retrieval.execution.routes import _run_classic_topk_route
    from shared.services.retrieval.publication_scope_content import (
        replace_document_scope_content as replace_scope_content,
    )
    from shared.services.retrieval.search.visual_discovery import (
        hybrid_discovery,
        visual_page_discovery,
    )
    from shared.services.storage.result_storage import JobResultStorage

    vec_p1 = [1.0] + [0.0] * (DEFAULT_EMBEDDING_DIM - 1)
    vec_p2 = [0.0, 1.0] + [0.0] * (DEFAULT_EMBEDDING_DIM - 2)
    monkeypatch.setattr(settings, "VISUAL_RETRIEVAL_ENABLED", True)
    monkeypatch.setattr(
        multimodal_client,
        "_call_gateway_embedding",
        lambda *, text=None, **kw: (
            vec_p2
            if "second page chart" in str(text or "")
            else vec_p1
        ),
    )

    fake_png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    fake_png_file = Path(tmp_path) / "page.png"
    fake_png_file.write_bytes(fake_png_bytes)

    monkeypatch.setattr(
        JobResultStorage,
        "download_raw_to_temp",
        lambda self, **kwargs: str(fake_png_file),
    )
    monkeypatch.setattr(
        JobResultStorage,
        "generate_artifact_url",
        lambda self, **kwargs: f"https://assets.knowhere.test/{kwargs.get('artifact_ref', 'page.png')}",
    )

    try:
        with Session(engine) as session:
            seed_publication_fixture(session, scope=scope)
            _set_job_progressive(
                session,
                job_id=scope.job_ref,
                ingestion_mode="progressive",
                enrichment_status="text",
            )
            session.commit()

        # Publish pure-visual M0 (empty content, _page_embedding + page_assets).
        m0_chunks: list[dict[str, object]] = [
            {
                "chunk_id": "scan-m0-p1",
                "type": "page",
                "content": "",
                "path": f"{scope.source_file_name}/p1",
                "metadata": {
                    "scope_id": "p1-1",
                    "enrichment_state": "text",
                    "page_nums": [1],
                    "_page_embedding": vec_p1,
                    "page_assets": [
                        {
                            "page_num": 1,
                            "artifact_ref": "page_citation_assets/page-1.png",
                            "content_type": "image/png",
                            "source": "knowhere-rendered-page-citation-source",
                        }
                    ],
                },
            },
            {
                "chunk_id": "scan-m0-p2",
                "type": "page",
                "content": "",
                "path": f"{scope.source_file_name}/p2",
                "metadata": {
                    "scope_id": "p2-2",
                    "enrichment_state": "text",
                    "page_nums": [2],
                    "_page_embedding": vec_p2,
                    "page_assets": [
                        {
                            "page_num": 2,
                            "artifact_ref": "page_citation_assets/page-2.png",
                            "content_type": "image/png",
                            "source": "knowhere-rendered-page-citation-source",
                        }
                    ],
                },
            },
        ]
        with Session(engine) as session:
            _finalize(session, job_id=scope.job_ref, chunks=m0_chunks)
            doc_id = str(
                session.execute(
                    select(Document.document_id).where(
                        Document.current_job_result_id == scope.revision_ref
                    )
                ).scalar_one()
            )
            session.commit()

            emb_rows = list(
                session.scalars(
                    select(DocumentPageEmbedding)
                    .where(DocumentPageEmbedding.document_id == doc_id)
                    .order_by(DocumentPageEmbedding.page_num)
                ).all()
            )
            assert len(emb_rows) == 2
            assert [r.chunk_id for r in emb_rows] == ["scan-m0-p1", "scan-m0-p2"]
            assert [r.page_num for r in emb_rows] == [1, 2]

            persisted_chunks = list(
                session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == doc_id)
                    .order_by(DocumentChunk.sort_order)
                ).all()
            )
            assert all(
                "_page_embedding" not in (c.chunk_metadata or {})
                for c in persisted_chunks
            )

        async def _verify_visual_m0_retrieval_and_tools() -> None:
            async with get_db_context() as async_db:
                # 1. Direct visual_page_discovery hits page 2 for query matching vec_p2.
                vis_res = await visual_page_discovery(
                    async_db,
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    query="what does the second page chart show",
                    top_k=2,
                )
                vis_rows = vis_res.payload["fused_rows"]
                assert len(vis_rows) == 2
                assert vis_rows[0]["chunk_id"] == "scan-m0-p2"

                # 2. Classic route answers via visual channel even though lexical is empty.
                classic_outcome = await _run_classic_topk_route(
                    RetrievalRouteContext(
                        db=async_db,
                        user_id=scope.user_ref,
                        namespace=scope.namespace_ref,
                        query="what does the second page chart show",
                        top_k=1,
                        effective_recall_k=2,
                        use_agentic=False,
                        exclude_document_ids=[],
                        exclude_sections=[],
                        chunk_types=None,
                        allowed_chunk_types=None,
                        rerank=False,
                        threshold=0.0,
                        internal_recall_k=2,
                        signal_paths=[],
                        filter_mode="delete",
                    )
                )
                classic_results = classic_outcome.response["results"]
                assert len(classic_results) == 1
                assert classic_results[0]["chunk_id"] == "scan-m0-p2"
                assert classic_results[0]["content_source"] == "page_image"
                assert classic_results[0]["page_nums"] == [2]
                assert any(
                    part.get("type") == "image"
                    for part in classic_results[0]["composed"]
                )

                # 3. Agent corpus.recall + corpus.read work on pure-visual M0.
                ctx = ToolContext(
                    db=async_db,
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    db_factory=get_db_context,
                )
                recall_res = await fresh_recall(
                    ctx, {"query": "what does the second page chart show"}
                )
                assert recall_res.error is None
                assert recall_res.payload["rows"][0]["section_path"] == "p2"
                assert "scanned page image" in recall_res.text

                grep_res = await fresh_grep(
                    ctx, {"pattern": "chart"}
                )
                assert grep_res.error is None
                assert "scanned document still awaiting background OCR" in grep_res.text
                assert "corpus.recall" in grep_res.text

                read_res = await fresh_read(
                    ctx,
                    {
                        "refs": [
                            {"document_id": doc_id, "chunk_id": "scan-m0-p2"}
                        ]
                    },
                )
                assert read_res.error is None
                assert "[Page image: https://assets.knowhere.test/page_citation_assets/page-2.png]" in read_res.text
                assert read_res.media == [
                    {
                        "type": "image_url",
                        "url": "https://assets.knowhere.test/page_citation_assets/page-2.png",
                    }
                ]

        asyncio.run(_verify_visual_m0_retrieval_and_tools())

        # 4. Background coarse + fine republish reuse M0 embeddings without recomputing,
        # rebind chunk_id to new section chunks, and inherit page_assets.
        with Session(engine) as session:
            pub_scope = DocumentPublicationScope(
                user_id=scope.user_ref,
                namespace=scope.namespace_ref,
                document_id=doc_id,
                job_result_id=scope.revision_ref,
                source_file_name=scope.source_file_name,
            )
            replace_scope_content(
                session,
                scope=pub_scope,
                scope_id="*",
                chunks=[
                    {
                        "chunk_id": "coarse-sec-1",
                        "type": "page",
                        "content": "ocr text page one",
                        "path": f"{scope.source_file_name}/Chapter1/SecA",
                        "metadata": {
                            "scope_id": "p1-1",
                            "enrichment_state": "coarse",
                            "page_nums": [1],
                        },
                    },
                    {
                        "chunk_id": "coarse-sec-2",
                        "type": "page",
                        "content": "ocr text page two",
                        "path": f"{scope.source_file_name}/Chapter1/SecB",
                        "metadata": {
                            "scope_id": "p2-2",
                            "enrichment_state": "coarse",
                            "page_nums": [2],
                        },
                    },
                ],
                section_summaries={"Chapter1 / SecB": "SecB summary"},
            )
            session.commit()

            emb_rows_after_coarse = list(
                session.scalars(
                    select(DocumentPageEmbedding)
                    .where(DocumentPageEmbedding.document_id == doc_id)
                    .order_by(DocumentPageEmbedding.page_num)
                ).all()
            )
            assert [r.chunk_id for r in emb_rows_after_coarse] == [
                "coarse-sec-1",
                "coarse-sec-2",
            ]
            sec2_chunk = session.execute(
                select(DocumentChunk).where(
                    DocumentChunk.document_id == doc_id,
                    DocumentChunk.chunk_id == "coarse-sec-2",
                )
            ).scalar_one()
            assert (sec2_chunk.chunk_metadata or {}).get("page_assets")

        async def _verify_hybrid_after_coarse() -> float:
            async with get_db_context() as async_db:
                hyb_res = await hybrid_discovery(
                    async_db,
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    query="second page chart",
                    top_k=2,
                )
                assert hyb_res.payload["fused_rows"][0]["chunk_id"] == "coarse-sec-2"

                vis_res = await visual_page_discovery(
                    async_db,
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    query="second page chart",
                    top_k=2,
                )
                vis_rows = vis_res.payload["fused_rows"]
                assert vis_rows[0]["chunk_id"] == "coarse-sec-2"

                excluded = await visual_page_discovery(
                    async_db,
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    query="second page chart",
                    top_k=2,
                    exclude_sections=[
                        {
                            "document_id": doc_id,
                            "section_path": "Chapter1 / SecB",
                        }
                    ],
                )
                assert "coarse-sec-2" not in [
                    row["chunk_id"] for row in excluded.payload["fused_rows"]
                ]
                return float(vis_rows[0]["score"])

        page2_score = asyncio.run(_verify_hybrid_after_coarse())

        with Session(engine) as session:
            replace_scope_content(
                session,
                scope=pub_scope,
                scope_id="*",
                chunks=[
                    {
                        "chunk_id": "merged-sec",
                        "type": "page",
                        "content": "ocr text page one and two",
                        "path": f"{scope.source_file_name}/Chapter1",
                        "metadata": {
                            "scope_id": "p1-1",
                            "enrichment_state": "coarse",
                            "page_nums": [1, 2],
                        },
                    }
                ],
            )
            session.commit()
            merged_emb_rows = list(
                session.scalars(
                    select(DocumentPageEmbedding)
                    .where(DocumentPageEmbedding.document_id == doc_id)
                    .order_by(DocumentPageEmbedding.page_num)
                ).all()
            )
            assert [r.chunk_id for r in merged_emb_rows] == [
                "merged-sec",
                "merged-sec",
            ]
            assert [r.page_num for r in merged_emb_rows] == [1, 2]

        async def _verify_chunk_max_pool_and_flag_off() -> None:
            async with get_db_context() as async_db:
                merged_res = await visual_page_discovery(
                    async_db,
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    query="second page chart",
                    top_k=2,
                )
                merged_rows = merged_res.payload["fused_rows"]
                assert len(merged_rows) == 1
                assert merged_rows[0]["chunk_id"] == "merged-sec"
                assert abs(float(merged_rows[0]["score"]) - page2_score) < 1e-9

                monkeypatch.setattr(settings, "VISUAL_RETRIEVAL_ENABLED", False)
                off_res = await visual_page_discovery(
                    async_db,
                    user_id=scope.user_ref,
                    namespace=scope.namespace_ref,
                    query="second page chart",
                    top_k=2,
                )
                assert off_res.payload["fused_rows"] == []

        asyncio.run(_verify_chunk_max_pool_and_flag_off())
    finally:
        engine.dispose()


