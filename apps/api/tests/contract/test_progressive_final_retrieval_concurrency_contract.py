"""A final same-revision publication must not produce mixed retrieval evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Connection, Engine, ExecutionContext, Result, make_url
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from shared.models.database.document import Document
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.publication_scope_content import (
    replace_document_scope_content,
)
from shared.services.retrieval.publication_service import RetrievalPublicationService
from shared.services.retrieval.serving_generation import (
    advance_namespace_generation,
)
from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)
from tests.support.publication_benchmark_support import ensure_benchmark_import_path
from tests.support.retrieval_snapshot_support import contract_db_session

ensure_benchmark_import_path()

from scripts.publication_benchmark.publication_execution import (  # noqa: E402
    PublicationScope,
    seed_publication_fixture,
)


def build_chunks(stage: str) -> list[dict[str, object]]:
    return [
        {
            "chunk_id": f"{stage}-{page}",
            "type": "page",
            "path": f"report.pdf/Chapter{page}",
            "content": f"revenue {stage} 111" if page == 1 else f"other topic {page}",
            "order": page,
            "metadata": {
                "page_nums": [page],
                "scope_id": f"p{page}-{page}",
                "enrichment_state": "text" if stage == "baseline" else "refined",
                "summary": f"revenue {stage} 999"
                if page == 1
                else f"other topic {page}",
            },
        }
        for page in range(1, 4)
    ]


@pytest.mark.parametrize(
    "boundary",
    ["before_read", "units", "hydration", "agent", "assembled", "partial", "no_hits", "small_growth", "small_archive"],
)
@pytest.mark.parametrize("mutation_count", [0, 1, 2])
def test_query_handles_final_publication_crossing_a_serving_read(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
    boundary: Literal["before_read", "units", "hydration", "agent", "assembled", "partial", "no_hits", "small_growth", "small_archive"],
    mutation_count: int,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    from shared.services.retrieval.execution import plan
    from shared.services.retrieval.execution import routes
    from shared.services.retrieval.app_service import run_retrieval_query
    from shared.core.exceptions.domain_exceptions import ConflictException

    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    scope = PublicationScope(
        user_ref=f"concurrent-{boundary}",
        namespace_ref=f"concurrent-{boundary}",
        source_file_name="report.pdf",
        job_ref=f"job-{boundary}",
        revision_ref=f"result-{boundary}",
    )

    is_small_corpus: bool = boundary in ("small_growth", "small_archive")

    def build_publication_chunks(stage: str, publication: int = 0) -> list[dict[str, object]]:
        chunks = build_chunks(stage)
        if publication and boundary in ("hydration", "partial"):
            chunks[0]["path"] = f"report.pdf/RefinedChapter{publication}"
        if boundary == "partial":
            chunks[1]["chunk_id"] = "steady-2"
            chunks[1]["content"] = "revenue steady 222"
        return chunks[:1] if is_small_corpus and not publication else chunks

    with Session(engine) as db:
        seed_publication_fixture(db, scope=scope)
        published = RetrievalPublicationService().publish_document_state(
            db,
            job_id=scope.job_ref,
            job_result_id=scope.revision_ref,
            chunks=build_publication_chunks("baseline"),
            update_namespace_snapshot=False,
        )
        assert published is not None and published.document_id is not None
        document_id = published.document_id
        db.commit()

    async def miss_cache(**kwargs: object) -> tuple[None, None]:
        return None, None

    monkeypatch.setattr(plan, "_read_cached_response", miss_cache)
    monkeypatch.setattr(plan, "_schedule_hit_stats_update", lambda **kwargs: None)
    published_during_read: list[bool] = []

    def publish_final() -> None:
        with Session(engine) as db:
            document = db.execute(
                select(Document)
                .where(Document.document_id == document_id)
                .with_for_update()
            ).scalar_one()
            publication_scope = DocumentPublicationScope(
                user_id=scope.user_ref,
                namespace=scope.namespace_ref,
                document_id=document_id,
                job_result_id=scope.revision_ref,
                source_file_name="report.pdf",
            )
            if boundary == "small_archive":
                document.status = "archived"
                advance_namespace_generation(
                    db, user_id=scope.user_ref, namespace=scope.namespace_ref,
                )
            else:
                chunks = build_publication_chunks("final", len(published_during_read) + 1)
                replace_document_scope_content(
                    db,
                    scope=publication_scope,
                    scope_id="p1-1" if boundary == "partial" else "*",
                    chunks=chunks[:1] if boundary == "partial" else chunks,
                )
            db.commit()
        published_during_read.append(True)

    def pause_between_serving_reads(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        del cursor, parameters, context, executemany
        marker = (
            "SELECT DISTINCT scoped_units.*"
            if boundary == "units"
            else "FROM document_map_unit_indexes AS indexes"
        )
        if (
            is_small_corpus or boundary in ("before_read", "agent", "assembled", "no_hits")
            or connection.dialect.driver != "asyncpg"
            or marker not in statement
            or len(published_during_read) >= mutation_count
        ):
            return
        with ThreadPoolExecutor(max_workers=1) as writer:
            writer.submit(publish_final).result(timeout=15)

    event.listen(Engine, "after_cursor_execute", pause_between_serving_reads)

    if is_small_corpus:
        count_scoped_chunks = routes.count_scoped_chunks

        async def publish_after_count(db: AsyncSession, **kwargs: Any) -> int:
            count = await count_scoped_chunks(db, **kwargs)
            if count == 1 and not published_during_read and mutation_count:
                publish_final()
            return count

        monkeypatch.setattr(routes, "count_scoped_chunks", publish_after_count)

    if boundary in ("before_read", "assembled", "no_hits"):
        from shared.services.retrieval.execution.route_types import RetrievalRouteContext, RetrievalRouteOutcome

        original = plan.run_retrieval_route

        async def publish_around_route(context: RetrievalRouteContext) -> RetrievalRouteOutcome:
            if boundary == "before_read" and len(published_during_read) < mutation_count:
                publish_final()
            outcome = await original(context)
            if boundary != "before_read" and len(published_during_read) < mutation_count:
                publish_final()
            return outcome

        monkeypatch.setattr(plan, "run_retrieval_route", publish_around_route)

    if boundary == "assembled":
        from shared.services.retrieval.hydration import result_assembly

        execute = AsyncSession.execute
        compose = result_assembly.compose_evidence_parts
        snapshot_sessions: list[AsyncSession] = []

        async def track_read_snapshot(
            session: AsyncSession, statement: Any, *args: Any, **kwargs: Any,
        ) -> Result[Any]:
            result: Result[Any] = await execute(session, statement, *args, **kwargs)
            if "ISOLATION LEVEL REPEATABLE READ, READ ONLY" in str(statement):
                snapshot_sessions.append(session)
            return result

        def compose_after_snapshot(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            # Composition may read citation images from object storage.
            assert snapshot_sessions
            assert all(not session.in_transaction() for session in snapshot_sessions)
            return compose(*args, **kwargs)

        monkeypatch.setattr(AsyncSession, "execute", track_read_snapshot)
        monkeypatch.setattr(result_assembly, "compose_evidence_parts", compose_after_snapshot)

    if boundary == "agent":
        from shared.services.retrieval.agent_explore import harness
        from shared.services.retrieval.agent_explore.budget import EpisodeBudget
        from shared.services.retrieval.agent_explore.dispatch import DbFactory
        from shared.services.retrieval.agent_explore.evidence_pool import Candidate
        from shared.services.retrieval.agent_explore.types import EpisodeResult
        from shared.services.retrieval.agent_tools.registry import ToolContext
        from shared.services.retrieval.agent_tools.tools.recall import recall
        from shared.services.retrieval.document_scope import DocumentScope

        class Harness:
            async def run_episode(
                self,
                *,
                db_factory: DbFactory,
                user_id: str,
                namespace: str,
                query: str,
                budget: EpisodeBudget,
                document_scope: DocumentScope,
            ) -> EpisodeResult:
                async with db_factory() as db:
                    found = await recall(
                        ToolContext(
                            db=db,
                            user_id=user_id,
                            namespace=namespace,
                            db_factory=db_factory,
                            document_scope=document_scope,
                        ),
                        {"query": query, "limit": 1},
                    )
                    assert found.refs
                    reference = found.refs[0]
                if len(published_during_read) < mutation_count:
                    await asyncio.to_thread(publish_final)
                return EpisodeResult(
                    pool=[
                        Candidate(
                            handle="read-1",
                            kind="read",
                            document_id=str(reference["document_id"]),
                            source_file_name="report.pdf",
                            chunk_type="page",
                            chunk_ids=(str(reference["chunk_id"]),),
                        )
                    ]
                )

        monkeypatch.setattr(harness, "resolve_harness", lambda **kwargs: Harness())
        monkeypatch.setattr(routes, "open_fresh_database_context", contract_db_session)
        monkeypatch.setattr(
            routes, "open_agent_explore_database_context", contract_db_session
        )

    async def query() -> Mapping[str, object]:
        async with contract_db_session() as db:
            return await run_retrieval_query(
                db=db,
                user_id=scope.user_ref,
                namespace=scope.namespace_ref,
                query="nonmatchingterm" if boundary == "no_hits" else "revenue",
                top_k=2 if boundary == "partial" else 1,
                internal_recall_k=3,
                exclude_document_ids=[],
                exclude_sections=[],
                use_agentic=boundary == "agent",
            )

    try:
        if boundary in ("agent", "before_read") and mutation_count == 2:
            with pytest.raises(ConflictException):
                asyncio.run(query())
            assert published_during_read == [True, True]
            return
        response = asyncio.run(query())
        results = response["results"]
        assert isinstance(results, list)
        if is_small_corpus and mutation_count:
            assert published_during_read == [True]
            assert response["router_used"] == "small_corpus_all"
            assert [row["chunk_id"] for row in results] == ["baseline-1"]
            assert results[0]["source"]["document_id"] == document_id
            assert results[0]["source"]["page_nums"] == [1]
            next_response = asyncio.run(query())
            if boundary == "small_growth":
                assert next_response["router_used"] == "classic_topk"
                next_results = next_response["results"]
                assert isinstance(next_results, list)
                assert [row["chunk_id"] for row in next_results] == ["final-1"]
            else:
                assert next_response["results"] == []
            return
        if boundary == "no_hits":
            assert published_during_read == [True] * min(mutation_count, 1)
            assert results == []
            return
        assert published_during_read == [True] * min(mutation_count, 1)
        assert results, "Concurrent final publication must not silently lose all matching evidence"
        expected_stage = "final" if boundary in ("agent", "before_read") and mutation_count else "baseline"
        expected_ids = {"baseline-1", "steady-2"} if boundary == "partial" else {f"{expected_stage}-1"}
        assert {row["chunk_id"] for row in results} == expected_ids
        if boundary == "partial":
            return
        assert expected_stage in results[0]["content"]
        assert results[0]["source"]["document_id"] == document_id
        assert results[0]["source"]["job_result_id"] == scope.revision_ref
        assert results[0]["source"]["page_nums"] == [1]
        references: object = response.get("referenced_chunks", [])
        assert isinstance(references, list)
        if boundary == "agent":
            assert references
        for reference in references:
            assert isinstance(reference, dict)
            assert reference["job_result_id"] == scope.revision_ref
            assert reference["page_nums"] == [1]
    finally:
        event.remove(Engine, "after_cursor_execute", pause_between_serving_reads)
        engine.dispose()


def test_final_generation_validation_fails_closed_when_database_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    from shared.services.retrieval.execution.revision_pins import (
        RetrievalRevisionPins,
        is_revision_generation_stable,
    )

    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE retrieval_namespace_generations RENAME TO unavailable_generations"
            )
        )

    async def validate() -> None:
        async with contract_db_session() as db:
            with pytest.raises(SQLAlchemyError):
                await is_revision_generation_stable(
                    db,
                    user_id="validation-user",
                    namespace="validation-namespace",
                    pins=RetrievalRevisionPins(revisions={}, generation=0),
                    strict=True,
                )
            assert not db.in_transaction()

    try:
        asyncio.run(validate())
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE unavailable_generations RENAME TO retrieval_namespace_generations"
                )
            )
        engine.dispose()


def test_empty_namespace_with_an_existing_generation_remains_searchable(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    from shared.services.retrieval.app_service import run_retrieval_query
    from shared.services.retrieval.execution import plan

    scope = PublicationScope(
        user_ref="empty-generation",
        namespace_ref="empty-generation",
        source_file_name="empty.pdf",
        job_ref="empty-generation-job",
        revision_ref="empty-generation-result",
    )
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    with Session(engine) as db:
        seed_publication_fixture(db, scope=scope)
        advance_namespace_generation(
            db, user_id=scope.user_ref, namespace=scope.namespace_ref
        )
        db.commit()

    async def miss_cache(**kwargs: object) -> tuple[None, None]:
        return None, None

    monkeypatch.setattr(plan, "_read_cached_response", miss_cache)
    monkeypatch.setattr(plan, "_schedule_hit_stats_update", lambda **kwargs: None)

    async def query() -> None:
        async with contract_db_session() as db:
            response = await run_retrieval_query(
                db=db,
                user_id=scope.user_ref,
                namespace=scope.namespace_ref,
                query="revenue",
                top_k=1,
                exclude_document_ids=[],
                exclude_sections=[],
                use_agentic=False,
            )
            assert response["results"] == []

    try:
        asyncio.run(query())
    finally:
        engine.dispose()


def test_empty_scope_capture_precedes_a_concurrent_first_publication(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    from shared.services.retrieval.execution.revision_pins import (
        RetrievalRevisionPins,
        capture_revision_pins,
    )

    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    scope = PublicationScope(
        user_ref="first-publication-capture",
        namespace_ref="first-publication-capture",
        source_file_name="first.pdf",
        job_ref="first-publication-capture-job",
        revision_ref="first-publication-capture-result",
    )
    engine: Engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    document_id: str | None = None

    def publish() -> str:
        with Session(engine) as db:
            seed_publication_fixture(db, scope=scope)
            published = RetrievalPublicationService().publish_document_state(
                db,
                job_id=scope.job_ref,
                job_result_id=scope.revision_ref,
                chunks=build_chunks("baseline"),
                update_namespace_snapshot=False,
            )
            assert published is not None and published.document_id is not None
            assert advance_namespace_generation(
                db, user_id=scope.user_ref, namespace=scope.namespace_ref,
            ) == 1
            db.commit()
            return published.document_id

    def publish_after_scope_select(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        nonlocal document_id
        if (
            document_id is None
            and connection.dialect.driver == "asyncpg"
            and "documents.current_job_result_id" in statement
        ):
            with ThreadPoolExecutor(max_workers=1) as writer:
                document_id = writer.submit(publish).result(timeout=15)

    async def capture() -> RetrievalRevisionPins:
        async with contract_db_session() as db:
            return await capture_revision_pins(
                db, user_id=scope.user_ref, namespace=scope.namespace_ref,
            )

    event.listen(Engine, "after_cursor_execute", publish_after_scope_select)
    try:
        before: RetrievalRevisionPins = asyncio.run(capture())
        after: RetrievalRevisionPins = asyncio.run(capture())
        assert document_id is not None
        assert dict(before) == {}
        assert before.generation == 0
        assert dict(after) == {document_id: scope.revision_ref}
        assert after.generation == 1
    finally:
        event.remove(Engine, "after_cursor_execute", publish_after_scope_select)
        engine.dispose()


def test_namespace_generation_primary_keys_do_not_alias_distinct_scopes(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    engine: Engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    try:
        with Session(engine) as db:
            first: int = advance_namespace_generation(db, user_id="owner:part", namespace="scope")
            db.commit()
        with Session(engine) as db:
            second: int = advance_namespace_generation(db, user_id="owner", namespace="part:scope")
            db.commit()
        assert (first, second) == (1, 1)
    finally:
        engine.dispose()


def test_simultaneous_first_publications_share_one_namespace_generation(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    from threading import Barrier
    from shared.models.database.document import RetrievalNamespaceGeneration

    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    engine: Engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    try:
        for attempt in range(4):
            barrier: Barrier = Barrier(2)
            namespace: str = f"first-publication-{attempt}"

            def publish() -> int:
                barrier.wait(timeout=5)
                with Session(engine) as db:
                    generation: int = advance_namespace_generation(
                        db, user_id="parallel-owner", namespace=namespace,
                    )
                    db.commit()
                    return generation

            with ThreadPoolExecutor(max_workers=2) as executor:
                publications = [executor.submit(publish) for _ in range(2)]
                assert sorted(task.result(timeout=10) for task in publications) == [1, 2]
            with Session(engine) as db:
                rows = db.scalars(select(RetrievalNamespaceGeneration).where(
                    RetrievalNamespaceGeneration.user_id == "parallel-owner",
                    RetrievalNamespaceGeneration.namespace == namespace,
                )).all()
                assert len(rows) == 1
                assert rows[0].generation == 2
    finally:
        engine.dispose()
