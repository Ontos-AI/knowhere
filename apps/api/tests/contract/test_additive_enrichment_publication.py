"""Exercise immutable enrichment bodies against the migrated database."""

import asyncio
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shared.models.database.document import Document, DocumentChunk
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
            )
            db.add(document)
            db.flush()
            scope = DocumentPublicationScope(
                user_id=fixture.user_ref, namespace=fixture.namespace_ref,
                source_file_name=fixture.source_file_name, document_id=document.document_id,
                job_result_id=fixture.revision_ref,
            )
            path = "same-file.pdf/Heading"
            db.add(DocumentChunk(
                id="original-row", chunk_id="original", document_id=scope.document_id,
                user_id=scope.user_id, namespace=scope.namespace, job_result_id=scope.job_result_id,
                source_chunk_path=path, content="Baseline spans original pages 1 through 3.",
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
            bodies = db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == scope.document_id)).all()
            assert {row.enrichment_attempt_id: row.content for row in bodies} == {
                None: "Baseline spans original pages 1 through 3.",
                "attempt-one": "Enriched page 1.",
                "attempt-two": "Replacement enrichment.",
            }
            assert {row.source_chunk_path for row in bodies} == {path}
            assert len({row.id for row in bodies}) == 3
            assert stored.state == "failed"
            assert not AdditivePublication.complete(db, scope=scope, attemptId="attempt-one")
    finally:
        engine.dispose()
