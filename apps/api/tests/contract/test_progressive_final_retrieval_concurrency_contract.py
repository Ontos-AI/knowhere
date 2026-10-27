"""A final same-revision publication must not produce mixed retrieval evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Connection, Engine, ExecutionContext, make_url
from sqlalchemy.orm import Session

from shared.models.database.document import Document
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.publication_scope_content import (
    replace_document_scope_content,
)
from shared.services.retrieval.publication_service import RetrievalPublicationService
from shared.services.retrieval.serving_generation import (
    advance_namespace_generation,
    lock_namespace_generation,
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


@pytest.mark.parametrize("boundary", ["units", "hydration", "agent"])
@pytest.mark.parametrize("mutation_count", [0, 1, 2])
def test_query_retries_if_final_publication_crosses_a_serving_read(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
    boundary: Literal["units", "hydration", "agent"],
    mutation_count: int,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    from shared.services.retrieval.execution import plan
    from shared.services.retrieval.execution import routes
    from shared.services.retrieval.app_service import run_retrieval_query
    from shared.services.retrieval.namespace_map_snapshot import (
        patch_namespace_map_snapshot,
    )
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
        document_id = published.document_id
        db.commit()

    async def miss_cache(**kwargs: object) -> tuple[None, None]:
        return None, None

    monkeypatch.setattr(plan, "_read_cached_response", miss_cache)
    monkeypatch.setattr(plan, "_schedule_hit_stats_update", lambda **kwargs: None)
    published_during_read: list[bool] = []

    def publish_final() -> None:
        with Session(engine) as db:
            generation = lock_namespace_generation(
                db, user_id=scope.user_ref, namespace=scope.namespace_ref
            )
            db.execute(
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
            payload = replace_document_scope_content(
                db, scope=publication_scope, scope_id="*", chunks=build_chunks("final")
            )
            patch_namespace_map_snapshot(
                db,
                scope=publication_scope,
                manifest_payload=payload,
                current_generation=generation,
            )
            advance_namespace_generation(
                db,
                user_id=scope.user_ref,
                namespace=scope.namespace_ref,
                locked_generation=generation,
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
            boundary == "agent"
            or connection.dialect.driver != "asyncpg"
            or marker not in statement
            or len(published_during_read) >= mutation_count
        ):
            return
        with ThreadPoolExecutor(max_workers=1) as writer:
            writer.submit(publish_final).result(timeout=15)

    event.listen(Engine, "after_cursor_execute", pause_between_serving_reads)

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
                query="revenue",
                top_k=1,
                internal_recall_k=3,
                exclude_document_ids=[],
                exclude_sections=[],
                use_agentic=boundary == "agent",
            )

    try:
        if mutation_count == 2:
            with pytest.raises(ConflictException):
                asyncio.run(query())
            assert published_during_read == [True, True]
            return
        response = asyncio.run(query())
        assert published_during_read == [True] * mutation_count
        results = response["results"]
        assert isinstance(results, list) and results, (
            "Concurrent final publication must not silently lose matching evidence"
        )
        expected_stage = "final" if mutation_count else "baseline"
        assert {row["chunk_id"] for row in results} == {f"{expected_stage}-1"}
        assert expected_stage in results[0]["content"]
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
