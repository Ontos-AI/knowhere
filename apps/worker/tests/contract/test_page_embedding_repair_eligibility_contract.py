"""Vector repair uses current published page ownership after coarse replacement."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shared.models.database.document import Document
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.schemas.progressive_ingestion import StoredSnapshot
from shared.services.jobs.progressive_ingestion import (
    initialize_baseline_state,
    read_progressive_state,
    write_progressive_state,
)
from shared.services.retrieval.page_embedding_index import (
    replace_document_page_embeddings,
)
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.services.retrieval.publication_service import RetrievalPublicationService
from shared.testing.contract_runtime import get_contract_database_url


def build_chunks(pages: list[int]) -> list[dict[str, object]]:
    return [
        {
            "chunk_id": f"eligibility-page-{page}",
            "type": "page",
            "path": f"doc.pdf/Page {page}",
            "content": f"Original page {page}",
            "metadata": {
                "page_nums": [page],
                "scope_id": f"p{page}-{page}",
                "enrichment_state": "coarse",
            },
        }
        for page in pages
    ]


def seed_revision(
    db: Session,
    *,
    owner_pages: list[int],
    persisted_pages: list[int],
    pending_pages: list[int],
    is_structure_running: bool,
) -> DocumentPublicationScope:
    identifier = uuid4().hex[:10]
    owner = f"eligibility-owner-{identifier}"
    job_id = f"eligibility-job-{identifier}"
    revision_id = str(uuid4())
    namespace = f"eligibility-{identifier}"
    db.execute(
        text('INSERT INTO "user" (id,name,email) VALUES (:owner,:name,:email)'),
        {
            "owner": owner,
            "name": "Eligibility contract",
            "email": f"{owner}@test.local",
        },
    )
    db.add(
        Job(
            job_id=job_id,
            user_id=owner,
            job_type="document_ingestion",
            status="done",
            source_type="file",
            webhook_enabled=False,
            job_metadata={
                "namespace": namespace,
                "parse_track": "page_memory",
                "source_file_name": "doc.pdf",
                "parsing_params": {"top_summary_use_llm": False},
            },
        )
    )
    db.flush()
    db.add(
        JobResult(
            id=revision_id,
            job_id=job_id,
            delivery_mode="url",
            result_s3_key="baseline.zip",
            result_size=10,
            inline_payload={"checksum": "baseline"},
        )
    )
    db.flush()
    published = RetrievalPublicationService().publish_document_state(
        db,
        job_id=job_id,
        job_result_id=revision_id,
        chunks=build_chunks(owner_pages),
        update_namespace_snapshot=False,
    )
    assert published.document_id is not None
    scope = DocumentPublicationScope(
        user_id=owner,
        namespace=namespace,
        document_id=published.document_id,
        job_result_id=revision_id,
        source_file_name="doc.pdf",
    )
    replace_document_page_embeddings(
        db,
        scope=scope,
        incoming_embeddings_by_page={page: [0.1] * 3072 for page in persisted_pages},
    )
    state = initialize_baseline_state(
        snapshot=StoredSnapshot(
            snapshot_id="baseline",
            stage="baseline",
            job_id=job_id,
            job_result_id=revision_id,
            document_id=scope.document_id,
            checksum="a" * 64,
            archive_key="baseline.zip",
        ),
        missing_pages=pending_pages,
    )
    state.stages["structure"] = {
        **state.stages["structure"],
        "status": "running" if is_structure_running else "complete",
        "claim_generation": "coarse-claim",
    }
    revision = db.get(JobResult, revision_id)
    assert revision is not None
    revision.document_metadata = write_progressive_state(
        {
            **dict(revision.document_metadata or {}),
            "structure_claim_generation": "coarse-claim",
        },
        state,
    )
    db.commit()
    return scope


def read_fingerprints(db: Session, scope: DocumentPublicationScope) -> dict[int, tuple]:
    rows = db.execute(
        text(
            "SELECT page_num,id,model,dims,md5(embedding::text) FROM document_page_embeddings WHERE user_id=:owner AND namespace=:namespace AND document_id=:document AND job_result_id=:revision ORDER BY page_num"
        ),
        {
            "owner": scope.user_id,
            "namespace": scope.namespace,
            "document": scope.document_id,
            "revision": scope.job_result_id,
        },
    ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


@pytest.mark.parametrize("has_missing_eligible_page", [False, True])
def test_coarse_publication_drops_excluded_pages_from_pending_repairs(
    worker_contract_environment: None,
    has_missing_eligible_page: bool,
) -> None:
    from app.services.page_memory import scope_enrichment_service as service
    from shared.services.retrieval.publication_scope_content import (
        replace_document_scope_content,
    )

    del worker_contract_environment
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    try:
        with Session(engine) as db:
            scope = seed_revision(
                db,
                owner_pages=[1, 2, 3, 4],
                persisted_pages=[1, 2, 4]
                if has_missing_eligible_page
                else [1, 2, 3, 4],
                pending_pages=[1, 2, 3, 4],
                is_structure_running=True,
            )
            revision = db.get(JobResult, scope.job_result_id)
            job = db.get(Job, revision.job_id)
            target = service._build_claimed_target(
                document=db.get(Document, scope.document_id),
                job=job,
                job_metadata=dict(job.job_metadata or {}),
                source_file_name="doc.pdf",
                claim_generation="coarse-claim",
            )
            before = read_fingerprints(db, scope)
        assert service._publish_coarse_structure_and_write_scopes(
            job_result_id=scope.job_result_id,
            target=target,
            publication_scope=scope,
            chunks=build_chunks([1, 3]),
            section_summaries={},
            phase_b_refs=[],
            scope_publisher=replace_document_scope_content,
        )
        with Session(engine) as db:
            state = read_progressive_state(
                db.get(JobResult, scope.job_result_id).document_metadata
            )
            repair = state.stages["vector_repair"]
            assert repair["pending_pages"] == (
                [3] if has_missing_eligible_page else []
            ), (
                "TOC/omitted pages must not become provider failures after their current chunk owners disappear"
            )
            assert repair["status"] == (
                "pending" if has_missing_eligible_page else "complete"
            )
            after = read_fingerprints(db, scope)
            assert set(after) == ({1} if has_missing_eligible_page else {1, 3})
            assert after[1] == before[1]
    finally:
        engine.dispose()


@pytest.mark.parametrize("has_missing_eligible_page", [False, True])
def test_recovery_ignores_stale_excluded_pages_but_repairs_missing_body_pages(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    has_missing_eligible_page: bool,
) -> None:
    from app.services.page_memory import page_renderer
    from app.services.page_memory import scope_enrichment_service as service
    from shared.services.ai import embedding

    del worker_contract_environment
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    rendered: list[list[int]] = []
    downloads: list[bool] = []

    def render(**arguments):
        pages = list(arguments["pages"])
        rendered.append(pages)
        return [
            SimpleNamespace(page_index=page, image_path=f"page-{page}.png")
            for page in pages
        ]

    def download(**_arguments):
        downloads.append(True)
        return "unused-source.pdf"

    monkeypatch.setattr(page_renderer, "render_document_pages", render)
    monkeypatch.setattr(
        embedding, "embed_images", lambda paths, **_: [[0.5] * 3072 for _path in paths]
    )
    try:
        with Session(engine) as db:
            scope = seed_revision(
                db,
                owner_pages=[1, 3],
                persisted_pages=[1] if has_missing_eligible_page else [1, 3],
                pending_pages=[2, 3, 4] if has_missing_eligible_page else [2, 4],
                is_structure_running=False,
            )
            revision = db.get(JobResult, scope.job_result_id)
            job = db.get(Job, revision.job_id)
            target = service._build_claimed_target(
                document=db.get(Document, scope.document_id),
                job=job,
                job_metadata=dict(job.job_metadata or {}),
                source_file_name="doc.pdf",
            )
            before = read_fingerprints(db, scope)
        service._repair_missing_page_embeddings(
            job_result_id=scope.job_result_id,
            target=target,
            storage=SimpleNamespace(download_raw_to_temp=download),
            workspace_dir=str(tmp_path),
        )
        with Session(engine) as db:
            state = read_progressive_state(
                db.get(JobResult, scope.job_result_id).document_metadata
            )
            assert state.stages["vector_repair"]["status"] == "complete", (
                "Successful provider results must not fail because excluded physical pages have no published owner"
            )
            assert state.stages["vector_repair"]["pending_pages"] == []
            after = read_fingerprints(db, scope)
            assert set(after) == {1, 3}
            assert after[1] == before[1]
        assert rendered == ([[3]] if has_missing_eligible_page else [])
        assert downloads == ([True] if has_missing_eligible_page else [])
    finally:
        engine.dispose()


@pytest.mark.parametrize("does_owner_disappear", [False, True])
def test_provider_failure_settles_against_current_published_owners(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    does_owner_disappear: bool,
) -> None:
    from app.services.page_memory import page_renderer
    from app.services.page_memory import scope_enrichment_service as service
    from shared.services.ai import embedding
    from shared.services.retrieval.publication_scope_content import (
        replace_document_scope_content,
    )

    del worker_contract_environment
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    try:
        with Session(engine) as db:
            scope = seed_revision(
                db,
                owner_pages=[1, 3],
                persisted_pages=[1],
                pending_pages=[3],
                is_structure_running=False,
            )
            revision = db.get(JobResult, scope.job_result_id)
            job = db.get(Job, revision.job_id)
            target = service._build_claimed_target(
                document=db.get(Document, scope.document_id),
                job=job,
                job_metadata=dict(job.job_metadata or {}),
                source_file_name="doc.pdf",
            )
            before = read_fingerprints(db, scope)

        def fail_provider(paths, **_arguments):
            assert paths == ["page-3.png"]
            if does_owner_disappear:
                with Session(engine) as db:
                    db.execute(
                        select(Document)
                        .where(Document.document_id == scope.document_id)
                        .with_for_update()
                    ).scalar_one()
                    replace_document_scope_content(
                        db, scope=scope, scope_id="p3-3", chunks=[]
                    )
                    db.commit()
            raise RuntimeError("contract provider failure")

        monkeypatch.setattr(
            page_renderer,
            "render_document_pages",
            lambda **_: [SimpleNamespace(page_index=3, image_path="page-3.png")],
        )
        monkeypatch.setattr(embedding, "embed_images", fail_provider)
        service._repair_missing_page_embeddings(
            job_result_id=scope.job_result_id,
            target=target,
            storage=SimpleNamespace(download_raw_to_temp=lambda **_: "unused.pdf"),
            workspace_dir=str(tmp_path),
        )
        with Session(engine) as db:
            state = read_progressive_state(
                db.get(JobResult, scope.job_result_id).document_metadata
            )
            repair = state.stages["vector_repair"]
            assert repair["pending_pages"] == ([] if does_owner_disappear else [3]), (
                "Provider-time ownership changes must be rechecked at authoritative settlement"
            )
            assert repair["status"] == (
                "complete" if does_owner_disappear else "failed"
            )
            assert (repair["error"] is None) == does_owner_disappear
            assert read_fingerprints(db, scope)[1] == before[1]
    finally:
        engine.dispose()


def test_empty_probe_cannot_complete_when_a_missing_owner_arrives_before_settlement(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services.page_memory import scope_enrichment_service as service
    from shared.services.retrieval.publication_scope_content import (
        replace_document_scope_content,
    )

    del worker_contract_environment
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    try:
        with Session(engine) as db:
            scope = seed_revision(
                db,
                owner_pages=[1],
                persisted_pages=[1],
                pending_pages=[2, 4],
                is_structure_running=False,
            )
            revision = db.get(JobResult, scope.job_result_id)
            job = db.get(Job, revision.job_id)
            target = service._build_claimed_target(
                document=db.get(Document, scope.document_id),
                job=job,
                job_metadata=dict(job.job_metadata or {}),
                source_file_name="doc.pdf",
            )
        finish = service._finish_progressive_stage
        added: list[bool] = []

        def add_owner_then_finish(**arguments):
            assert (
                arguments["stage_name"] == "vector_repair"
                and arguments["pending_pages"] == []
            )
            with Session(engine) as db:
                db.execute(
                    select(Document)
                    .where(Document.document_id == scope.document_id)
                    .with_for_update()
                ).scalar_one()
                replace_document_scope_content(
                    db, scope=scope, scope_id="p3-3", chunks=build_chunks([3])
                )
                db.commit()
            added.append(True)
            return finish(**arguments)

        monkeypatch.setattr(service, "_finish_progressive_stage", add_owner_then_finish)

        def reject_provider(**_arguments):
            pytest.fail("The empty initial coverage probe must not call the provider")

        service._repair_missing_page_embeddings(
            job_result_id=scope.job_result_id,
            target=target,
            storage=SimpleNamespace(download_raw_to_temp=reject_provider),
            workspace_dir=str(tmp_path),
        )
        assert added == [True]
        with Session(engine) as db:
            repair = read_progressive_state(
                db.get(JobResult, scope.job_result_id).document_metadata
            ).stages["vector_repair"]
            assert repair["status"] == "failed" and repair["pending_pages"] == [3], (
                "A newly published eligible owner still requires its missing vector"
            )
            assert repair["error"] is not None
    finally:
        engine.dispose()


def test_vector_settlement_cannot_update_an_archived_document(
    worker_contract_environment: None,
) -> None:
    from app.services.page_memory import scope_enrichment_service as service

    del worker_contract_environment
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    try:
        with Session(engine) as db:
            scope = seed_revision(
                db,
                owner_pages=[1],
                persisted_pages=[1],
                pending_pages=[2],
                is_structure_running=False,
            )
        token = service._claim_progressive_stage(
            job_result_id=scope.job_result_id, stage_name="vector_repair"
        )
        assert token is not None
        with Session(engine) as db:
            document = db.get(Document, scope.document_id)
            document.status = "archived"
            db.commit()
        assert not service._finish_progressive_stage(
            job_result_id=scope.job_result_id,
            stage_name="vector_repair",
            claim_generation=token,
            error=None,
            pending_pages=[],
        )
        with Session(engine) as db:
            repair = read_progressive_state(
                db.get(JobResult, scope.job_result_id).document_metadata
            ).stages["vector_repair"]
            assert repair["status"] == "running" and repair["claim_generation"] == token
    finally:
        engine.dispose()
