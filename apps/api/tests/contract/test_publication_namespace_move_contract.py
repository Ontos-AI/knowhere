"""Publication moves one document's graph and snapshot between namespaces."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shared.core.config import settings
from shared.models.database.document import (
    GraphEdge,
    GraphNode,
    RetrievalNamespaceGeneration,
    RetrievalNamespaceMapSnapshot,
)
from shared.models.database.job import Job
from shared.services.retrieval.graph.service import DocumentGraphService
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.publication_service import RetrievalPublicationService
from shared.services.retrieval.serving_manifest import decode_namespace_map_snapshot
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


def _scope(namespace: str, revision: str) -> PublicationScope:
    return PublicationScope(
        user_ref="namespace-move-user",
        namespace_ref=namespace,
        source_file_name="move-source.pdf",
        job_ref=f"namespace-move-job-{revision}",
        revision_ref=f"namespace-move-result-{revision}",
    )


def _chunks() -> list[dict[str, object]]:
    return [
        {
            "chunk_id": "namespace-move-content",
            "type": "text",
            "content": "A document that moves between namespaces",
            "path": "move-source.pdf/Chapter/Body",
            "metadata": {},
        }
    ]


def _keyword_chunks(chunk_id: str) -> list[dict[str, object]]:
    return [
        {
            "chunk_id": chunk_id,
            "type": "text",
            "content": "alpha beta gamma graph candidate",
            "path": "graph-source.pdf/Root/Body",
            "metadata": {"keywords": ["alpha", "beta", "gamma"]},
        }
    ]


@pytest.mark.parametrize("strategy", ("baseline", "candidate"))
def test_replacement_moves_graph_and_snapshot_in_one_transaction(
    strategy: str,
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    monkeypatch.setattr(settings, "KNOWHERE_PUBLICATION_STRATEGY", strategy)
    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    service = RetrievalPublicationService()
    first = _scope("first-namespace", "first")
    second = _scope("second-namespace", "second")
    try:
        with Session(engine) as session:
            seed_publication_fixture(session, scope=first)
            session.commit()
            first_state = service.publish_document_state(
                session,
                job_id=first.job_ref,
                job_result_id=first.revision_ref,
                chunks=_chunks(),
                update_namespace_snapshot=True,
            )
            assert first_state is not None
            assert first_state.document_id is not None
            service.publish_document_graph(
                session, job_id=first.job_ref, job_result_id=first.revision_ref
            )
            session.commit()

            seed_publication_fixture(session, scope=second)
            job = session.scalar(select(Job).where(Job.job_id == second.job_ref))
            assert job is not None
            job.job_metadata = {**(job.job_metadata or {}), "document_id": first_state.document_id}
            session.commit()

            second_state = service.publish_document_state(
                session,
                job_id=second.job_ref,
                job_result_id=second.revision_ref,
                chunks=_chunks(),
                update_namespace_snapshot=False,
            )
            assert second_state is not None
            assert second_state.document_id == first_state.document_id
            assert second_state.previous_namespace == first.namespace_ref
            assert second_state.manifest_payload is not None
            with pytest.raises(ValueError, match="Graph publication scope"):
                DocumentGraphService().publish_document_graph(
                    session,
                    user_id=second.user_ref,
                    namespace=first.namespace_ref,
                    document_id=first_state.document_id,
                    job_result_id=second.revision_ref,
                )
            service.publish_document_graph(
                session, job_id=second.job_ref, job_result_id=second.revision_ref
            )
            service.update_namespace_snapshot(
                session,
                scope=DocumentPublicationScope(
                    user_id=second.user_ref,
                    namespace=second.namespace_ref,
                    document_id=first_state.document_id,
                    job_result_id=second.revision_ref,
                    source_file_name=second.source_file_name,
                ),
                manifest_payload=second_state.manifest_payload,
                previous_namespace=second_state.previous_namespace,
            )
            session.commit()
            graph_node = session.get(GraphNode, f"doc:{first_state.document_id}")
            assert graph_node is not None
            assert graph_node.namespace == second.namespace_ref
            for namespace, expected_document_ids in (
                (first.namespace_ref, set()),
                (second.namespace_ref, {first_state.document_id}),
            ):
                snapshot = session.scalar(
                    select(RetrievalNamespaceMapSnapshot).where(
                        RetrievalNamespaceMapSnapshot.user_id == first.user_ref,
                        RetrievalNamespaceMapSnapshot.namespace == namespace,
                    )
                )
                assert snapshot is not None
                payload = decode_namespace_map_snapshot(
                    snapshot.payload_zlib,
                    checksum=snapshot.checksum,
                    format_version=snapshot.format_version,
                )
                assert set(payload["documents"]) == expected_document_ids
    finally:
        engine.dispose()


def test_graph_publication_filters_peers_by_keyword_candidates(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    monkeypatch.setattr(settings, "KNOWHERE_PUBLICATION_STRATEGY", "candidate")
    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    service = RetrievalPublicationService()
    first = PublicationScope(
        user_ref="graph-filter-user",
        namespace_ref="graph-filter-namespace",
        source_file_name="graph-first.pdf",
        job_ref="graph-filter-job-first",
        revision_ref="graph-filter-result-first",
        document_id_ref="doc_graph_filter_first",
    )
    second = PublicationScope(
        user_ref="graph-filter-user",
        namespace_ref="graph-filter-namespace",
        source_file_name="graph-second.pdf",
        job_ref="graph-filter-job-second",
        revision_ref="graph-filter-result-second",
        document_id_ref="doc_graph_filter_second",
    )
    try:
        with Session(engine) as session:
            for scope in (first, second):
                seed_publication_fixture(session, scope=scope)
            session.commit()

            for scope in (first, second):
                published = service.publish_document_state(
                    session,
                    job_id=scope.job_ref,
                    job_result_id=scope.revision_ref,
                    chunks=_keyword_chunks(scope.document_id_ref or scope.job_ref),
                    update_namespace_snapshot=False,
                )
                assert published is not None
                service.publish_document_graph(
                    session,
                    job_id=scope.job_ref,
                    job_result_id=scope.revision_ref,
                )
                session.commit()

            edges = session.scalars(
                select(GraphEdge).where(
                    GraphEdge.user_id == first.user_ref,
                    GraphEdge.namespace == first.namespace_ref,
                )
            ).all()
            assert len(edges) == 1
            assert {
                edges[0].source_node_id,
                edges[0].target_node_id,
            } == {"doc:doc_graph_filter_first", "doc:doc_graph_filter_second"}
    finally:
        engine.dispose()


def test_oversized_snapshot_uses_generation_mismatch_fallback(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    monkeypatch.setattr(settings, "KNOWHERE_PUBLICATION_STRATEGY", "candidate")
    monkeypatch.setattr(
        settings, "KNOWHERE_PUBLICATION_NAMESPACE_SNAPSHOT_MAX_BYTES", 1
    )
    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    service = RetrievalPublicationService()
    first = _scope("oversized-snapshot-namespace", "first")
    try:
        with Session(engine) as session:
            seed_publication_fixture(session, scope=first)
            session.commit()
            first_state = service.publish_document_state(
                session,
                job_id=first.job_ref,
                job_result_id=first.revision_ref,
                chunks=_chunks(),
                update_namespace_snapshot=True,
            )
            assert first_state is not None and first_state.document_id is not None
            session.commit()

            replacement = PublicationScope(
                user_ref=first.user_ref,
                namespace_ref=first.namespace_ref,
                source_file_name=first.source_file_name,
                job_ref="snap-job-repl",
                revision_ref="snap-result-repl",
                document_id_ref=first_state.document_id,
            )
            seed_publication_fixture(session, scope=replacement)
            session.commit()
            replacement_state = service.publish_document_state(
                session,
                job_id=replacement.job_ref,
                job_result_id=replacement.revision_ref,
                chunks=_chunks(),
                update_namespace_snapshot=True,
            )
            assert replacement_state is not None
            session.commit()

            snapshot = session.scalar(
                select(RetrievalNamespaceMapSnapshot).where(
                    RetrievalNamespaceMapSnapshot.user_id == first.user_ref,
                    RetrievalNamespaceMapSnapshot.namespace == first.namespace_ref,
                )
            )
            generation = session.scalar(
                select(RetrievalNamespaceGeneration).where(
                    RetrievalNamespaceGeneration.user_id == first.user_ref,
                    RetrievalNamespaceGeneration.namespace == first.namespace_ref,
                )
            )
            assert snapshot is not None and generation is not None
            assert snapshot.generation < generation.generation
    finally:
        engine.dispose()
