"""Worker stages publish searchable native builds for the same revision."""

from functools import partial
import os

import httpx
import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from shared.testing.contract_runtime import get_contract_database_url


@pytest.mark.parametrize("track", ["page_memory", "chunk"])
def test_worker_publishes_progressive_stages_to_opensearch(
    worker_contract_environment: None, monkeypatch: pytest.MonkeyPatch, track: str,
) -> None:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if not endpoint:
        pytest.skip("An isolated OpenSearch node is required")

    from app.services.document_ingestion import chunk_enrichment_service as chunk_service
    from app.services.page_memory import scope_enrichment_service as page_service
    from shared.core.config import settings
    from shared.models.database.document import Document
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.user import User
    from shared.models.schemas.progressive_ingestion import StoredSnapshot
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.worker import IndexingWorker
    from shared.services.retrieval.publication_scope_content import replace_document_scope_content

    monkeypatch.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_URL", endpoint)
    monkeypatch.setattr(settings, "KNOWHERE_OPENSEARCH_INDEX_PREFIX", "knowhere-progressive-worker")
    monkeypatch.setattr(LegacyCopyDispatch, "submit", staticmethod(lambda identity: False))

    namespace: str = f"progressive-{track}"
    owner_id: str = "progressive-contract-owner"
    document_id: str = "doc_progressive_contract"
    job_id: str = "progressive-contract-job"
    filename: str = "report.pdf" if track == "page_memory" else "report.md"
    scope = LexicalScope(ownerId=owner_id, namespace=namespace)
    native = OpenSearchLexicalIndex(endpoint=endpoint, scope=scope, indexPrefix="knowhere-progressive-worker")
    engine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))

    def search_stage(expected: str, previous: str) -> str:
        with Session(engine) as db:
            document = db.get(Document, document_id)
            assert document is not None and document.current_job_result_id == revision_id
            build_id = document.current_indexing_build_id
            assert build_id is not None
        assert native.search(buildIds=(build_id,), queryTokens=(expected,), limit=5)
        assert not native.search(buildIds=(build_id,), queryTokens=(previous,), limit=5)
        return build_id

    try:
        with Session(engine) as db, db.begin():
            db.add(User(id=owner_id, name="Progressive worker", email="progressive-worker@example.invalid"))
            db.flush()
            db.add(Job(job_id=job_id, user_id=owner_id, job_type="document_ingestion",
                source_type="file", status="running", job_metadata={
                    "document_id": document_id, "namespace": namespace, "source_file_name": filename,
                    "parse_track": track, "ingestion_mode": "progressive", "enrichment_status": "text",
                }))
            db.flush()
            parsed = SyncJobSuccessFinalizer().finalize(db, job_id=job_id, result_s3_key="results/baseline.zip",
                checksum="a" * 64, zip_size=100, stored_count=1, delivery_mode="url", section_summaries=None,
                chunks=[{"chunk_id": "baseline", "type": "text", "content": "quartzbaseline",
                    "path": f"{filename}/Baseline", "metadata": {}}])
            assert parsed.response.should_commit()
            revision_id: str = db.scalars(select(JobResult.id).where(JobResult.job_id == job_id)).one()

        assert IndexingWorker(sessionFactory=sessionmaker(engine, expire_on_commit=False)).run(job_id) == "completed"
        baseline_build: str = search_stage("quartzbaseline", "orchidrefined")
        if track == "page_memory":
            _, target = page_service._load_and_validate_structure_target(job_result_id=revision_id)
            assert target is not None
            assert page_service._publish_coarse_structure_and_write_scopes(
                job_result_id=revision_id, target=target,
                publication_scope=page_service._build_publication_scope(target=target, job_result_id=revision_id),
                chunks=[{"chunk_id": "coarse", "type": "page", "content": "cedarcoarse",
                    "path": f"{filename}/Coarse", "metadata": {
                        "scope_id": "p1-1", "page_nums": [1], "enrichment_state": "coarse",
                    }}],
                section_summaries={}, phase_b_refs=[], scope_publisher=replace_document_scope_content,
            )
            coarse_build: str = search_stage("cedarcoarse", "quartzbaseline")
            assert coarse_build != baseline_build
            _, chapter = page_service._claim_and_validate_scope(job_result_id=revision_id, scope_id="p1-1")
            assert chapter is not None
            assert page_service._publish_and_mark_refined(
                job_result_id=revision_id, scope_id="p1-1", target=chapter,
                output=page_service.ScopeRefinementOutput(
                    scope_id="p1-1", dataframe=pd.DataFrame(), section_summaries={}, artifact_refs=set(),
                    chunks=[{"chunk_id": "refined", "type": "page", "content": "orchidrefined",
                        "path": f"{filename}/Refined", "metadata": {"page_nums": [1]}}],
                ),
                scope_publisher=replace_document_scope_content,
            )
            assert search_stage("orchidrefined", "cedarcoarse") != coarse_build
        else:
            claim = page_service.claim_progressive_stage(job_result_id=revision_id, stage_name="finalization")
            assert claim is not None
            target = chunk_service._ChunkFinalTarget(document_id=document_id, job_id=job_id, user_id=owner_id,
                namespace=namespace, source_file_name=filename, s3_key="uploads/report.md",
                raw_prefix="results/baseline/", job_metadata={})
            snapshot = StoredSnapshot(snapshot_id="snapshot_final", stage="final", job_id=job_id,
                job_result_id=revision_id, document_id=document_id, checksum="b" * 64, archive_key="results/final.zip")
            chunk_service._publish_final_revision(job_result_id=revision_id, target=target,
                chunks=[{"chunk_id": "final", "type": "text", "content": "orchidrefined",
                    "path": f"{filename}/Final", "metadata": {}}], section_summaries={},
                complete=partial(chunk_service._complete_final_snapshot, target=target,
                    snapshot=snapshot, zip_size=120, claim_token=claim),
            )
            assert search_stage("orchidrefined", "quartzbaseline") != baseline_build
            with Session(engine) as db:
                result = db.get(JobResult, revision_id)
                assert result is not None and result.result_s3_key == "results/final.zip"
                assert (result.document_metadata or {}).get("result_raw_prefix") == "results/final/"
    finally:
        httpx.delete(f"{endpoint}/{native.indexName}", timeout=15, trust_env=False)
        native.close()
        engine.dispose()
