"""Phase B scope enrichment task contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.services.document_agent.manifest import (
    PageAnatomyMap,
    PageFeature,
    PageLabel,
    ShardPlan,
    TocResult,
)
from app.services.page_memory._utils import build_hierarchy_scopes
from app.services.page_memory.page_assets import PageAsset
from app.services.page_memory.page_renderer import PageRenderResult
from app.services.page_memory.page_tagger import PageTagResult
from app.services.page_memory.phase_a import (
    PHASE_B_ANATOMY_REL,
    PHASE_B_BODY_TEXT_REL,
    PHASE_B_FULL_TEXT_REL,
    PHASE_B_MANIFEST_REL,
    PHASE_B_SOURCE_PDF_REL,
    build_phase_a_manifest,
)
from app.services.page_memory.skeleton_extractor import SectionSkeleton
from shared.core.celery_app import celery_app
from shared.models.database.document import (
    Document,
    DocumentChunk,
    DocumentScopeEnrichment,
)
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.services.retrieval.publication_service import RetrievalPublicationService
from shared.testing.contract_runtime import (
    get_contract_database_url,
)


class _InMemoryResultStorage:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.uploaded_raw: dict[str, bytes] = {}
        self.uploaded_zips: list[str] = []

    def download_raw_to_temp(
        self,
        *,
        job_id: str,
        relative_path: str,
        raw_prefix: str | None = None,
        suffix: str,
        temp_dir: str,
    ) -> str:
        del job_id, raw_prefix
        if relative_path not in self.files:
            raise FileNotFoundError(relative_path)
        target = Path(temp_dir) / f"_dl_{Path(relative_path).name}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.files[relative_path])
        return str(target)

    def upload_raw_file(
        self,
        *,
        job_id: str,
        relative_path: str,
        local_file_path: str,
        raw_prefix: str | None = None,
    ) -> None:
        del job_id, raw_prefix
        payload = Path(local_file_path).read_bytes()
        self.uploaded_raw[relative_path] = payload
        self.files[relative_path] = payload

    def upload(
        self,
        *,
        job_id: str,
        result_dir: str,
        zip_file_path: str,
        artifact_refs: set[str] | None = None,
    ) -> Any:
        del result_dir, artifact_refs
        from shared.services.storage.result_storage import UploadedResultBundle

        zip_key = f"results/{job_id}.zip"
        self.uploaded_zips.append(zip_key)
        Path(zip_file_path).unlink(missing_ok=True)
        return UploadedResultBundle(
            zip_key=zip_key,
            raw_prefix=f"results/{job_id}/",
            raw_files={},
        )


def _seed_published_coarse_doc(
    session: Session,
    *,
    suffix: str,
    scope_states: list[tuple[str, int, int, str]],
) -> tuple[str, str, str]:
    from sqlalchemy import text

    user_id = f"enrich-user-{suffix}"
    job_id = f"enrich-job-{suffix}"
    revision_id = f"enrich-rev-{suffix}"
    session.execute(
        text(
            'INSERT INTO "user" (id, name, email) '
            "VALUES (:id, :name, :email) ON CONFLICT (id) DO NOTHING"
        ),
        {"id": user_id, "name": "Enrich User", "email": f"{user_id}@test.local"},
    )
    job = Job(
        job_id=job_id,
        user_id=user_id,
        job_type="document_ingestion",
        status="done",
        source_type="file",
        webhook_enabled=False,
        job_metadata={
            "namespace": f"ns-{suffix}",
            "parse_track": "page_memory",
            "source_file_name": "doc.pdf",
            "result_raw_prefix": f"results/{job_id}/",
            "enrichment_status": "coarse",
            "page_memory_phase_b": [
                PHASE_B_SOURCE_PDF_REL,
                PHASE_B_MANIFEST_REL,
                PHASE_B_ANATOMY_REL,
                PHASE_B_FULL_TEXT_REL,
                PHASE_B_BODY_TEXT_REL,
            ],
            "parsing_params": {"top_summary_use_llm": False},
        },
    )
    session.add(job)
    session.flush()
    job_result = JobResult(
        id=revision_id,
        job_id=job_id,
        delivery_mode="url",
        result_s3_key=f"results/{job_id}_coarse.zip",
        result_size=10,
        inline_payload={"checksum": "coarse"},
    )
    session.add(job_result)
    session.flush()

    published = RetrievalPublicationService().publish_document_state(
        session,
        job_id=job_id,
        job_result_id=revision_id,
        chunks=[
            {
                "chunk_id": f"coarse-{scope_id}",
                "type": "page",
                "content": f"raw {scope_id}",
                "path": f"doc.pdf/{scope_id}",
                "metadata": {
                    "scope_id": scope_id,
                    "enrichment_state": "coarse",
                    "page_nums": [page_start, page_end],
                },
            }
            for scope_id, page_start, page_end, _state in scope_states
        ],
        update_namespace_snapshot=False,
    )
    assert published is not None and published.document_id is not None
    for scope_id, page_start, page_end, state in scope_states:
        session.add(
            DocumentScopeEnrichment(
                document_id=published.document_id,
                job_result_id=revision_id,
                scope_id=scope_id,
                page_start=page_start,
                page_end=page_end,
                state=state,
            )
        )
    session.commit()
    return published.document_id, job_id, revision_id


def _build_storage_inputs() -> dict[str, bytes]:
    import json

    skeletons = [
        SectionSkeleton(
            section_path="doc.pdf/Parent",
            title="Parent",
            level=1,
            start_page=1,
            end_page=2,
            parent_path="doc.pdf",
        ),
        SectionSkeleton(
            section_path="doc.pdf/Parent/OnlyChild",
            title="OnlyChild",
            level=2,
            start_page=1,
            end_page=2,
            parent_path="doc.pdf/Parent",
        ),
        SectionSkeleton(
            section_path="doc.pdf/Parent/OnlyChild/GrandA",
            title="GrandA",
            level=3,
            start_page=1,
            end_page=2,
            parent_path="doc.pdf/Parent/OnlyChild",
        ),
        SectionSkeleton(
            section_path="doc.pdf/Parent/OnlyChild/GrandB",
            title="GrandB",
            level=3,
            start_page=1,
            end_page=2,
            parent_path="doc.pdf/Parent/OnlyChild",
        ),
        SectionSkeleton(
            section_path="doc.pdf/Other",
            title="Other",
            level=1,
            start_page=3,
            end_page=4,
            parent_path="doc.pdf",
        ),
    ]
    scopes = [
        build_hierarchy_scopes(
            skeletons=skeletons[:4],
            filename="doc.pdf",
            page_count=4,
        )[0],
        build_hierarchy_scopes(
            skeletons=skeletons[4:],
            filename="doc.pdf",
            page_count=4,
        )[0],
    ]
    manifest = build_phase_a_manifest(
        filename="doc.pdf",
        page_count=4,
        skeletons=skeletons,
        coarse_scopes=scopes,
        next_title_by_path={"doc.pdf/Parent/OnlyChild/GrandB": "Other"},
        toc_pages=[],
    )
    anatomy = PageAnatomyMap(
        job_id="j1",
        file_path="source.pdf",
        page_count=4,
        page_features=[
            PageFeature(
                page=page,
                raw_text_length=20,
                text_density=0.5,
                image_coverage=0.1,
                image_count=1,
                table_count=0,
                drawings_count=0,
                orientation="portrait",
                width=600.0,
                height=800.0,
                has_asset=(page == 1),
                is_blank_like=False,
            )
            for page in range(1, 5)
        ],
        page_labels=[PageLabel(page=page, kind="normal") for page in range(1, 5)],
        toc_result=TocResult(toc_pages=[]),
        shard_plan=ShardPlan(enabled=False, reason="not_needed"),
    )
    return {
        PHASE_B_SOURCE_PDF_REL: b"%PDF-1.4",
        PHASE_B_MANIFEST_REL: json.dumps(manifest).encode("utf-8"),
        PHASE_B_ANATOMY_REL: json.dumps(anatomy.to_dict()).encode("utf-8"),
        PHASE_B_FULL_TEXT_REL: json.dumps(
            {"1": "page one text", "2": "page two text", "3": "p3", "4": "p4"}
        ).encode("utf-8"),
        PHASE_B_BODY_TEXT_REL: json.dumps(
            {"1": "body one", "2": "body two", "3": "b3", "4": "b4"}
        ).encode("utf-8"),
    }


def _install_fake_scope_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    from PIL import Image

    def fake_render(
        *,
        output_dir: str,
        scope_id: str,
        pages: list[int],
        page_texts: dict[int, str],
        **kwargs: object,
    ) -> list[PageRenderResult]:
        del kwargs
        out: list[PageRenderResult] = []
        render_dir = Path(output_dir) / "pages" / scope_id
        render_dir.mkdir(parents=True, exist_ok=True)
        for page in pages:
            img_path = render_dir / f"page-{page}.png"
            Image.new("RGB", (40, 50), color="white").save(img_path)
            out.append(
                PageRenderResult(
                    page_index=page,
                    image_path=str(img_path),
                    raw_text=page_texts.get(page, ""),
                    width=600.0,
                    height=800.0,
                    is_landscape=False,
                )
            )
        return out

    def fake_tag(
        *, pages: list[PageRenderResult], **kwargs: object
    ) -> list[PageTagResult]:
        del kwargs
        return [
            PageTagResult(
                page_index=item.page_index,
                summary=f"summary p{item.page_index}",
                keywords=[f"kw{item.page_index}"],
                strategy_used="fake",
            )
            for item in pages
        ]

    def fake_assets(
        *,
        rendered_pages: list[PageRenderResult],
        output_dir: str,
        **kwargs: object,
    ) -> dict[int, list[PageAsset]]:
        del kwargs
        if not rendered_pages:
            return {}
        page = rendered_pages[0].page_index
        img_dir = Path(output_dir) / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        img_file = img_dir / f"page-{page}-fig-1.png"
        from PIL import Image as PilImage

        PilImage.new("RGB", (20, 20), color="blue").save(img_file)
        return {
            page: [
                PageAsset(
                    asset_id=f"asset_p{page}_1",
                    page_index=page,
                    asset_index=1,
                    kind="figure",
                    title="Figure 1",
                    summary="chart summary",
                    keywords=["chart"],
                    entities=[],
                    bbox_px=[0, 0, 20, 20],
                    width_px=40,
                    height_px=50,
                    width_pt=600.0,
                    height_pt=800.0,
                    image_uri=f"images/{img_file.name}",
                )
            ]
        }

    import app.services.page_memory.page_assets as page_assets_mod
    import app.services.page_memory.page_renderer as page_renderer_mod
    import app.services.page_memory.page_tagger as page_tagger_mod

    monkeypatch.setattr(page_renderer_mod, "render_document_pages", fake_render)
    monkeypatch.setattr(page_tagger_mod, "tag_pages", fake_tag)
    monkeypatch.setattr(
        page_assets_mod, "extract_page_assets_from_renders", fake_assets
    )


def test_enrichment_task_is_routed_to_low_queue_and_does_not_fail_job() -> None:
    from app.core.tasks.enrichment_tasks import (
        build_page_memory_structure,
        enrich_page_memory_scope,
    )

    assert enrich_page_memory_scope.name == (
        "app.core.tasks.enrichment_tasks.enrich_page_memory_scope"
    )
    assert build_page_memory_structure.name == (
        "app.core.tasks.enrichment_tasks.build_page_memory_structure"
    )
    assert (
        celery_app.conf.task_routes[
            "app.core.tasks.enrichment_tasks.build_page_memory_structure"
        ]
        == {"queue": "document_ingestion_medium"}
    )
    assert celery_app.conf.task_routes["app.core.tasks.enrichment_tasks.*"] == {
        "queue": "document_ingestion_low"
    }
    assert enrich_page_memory_scope.finalize_job_on_failure is False
    assert build_page_memory_structure.finalize_job_on_failure is False
    assert (
        enrich_page_memory_scope._extract_job_id(("rev-1", "p1-2"), {}) is None  # noqa: SLF001
    )


def test_execute_scope_enrichment_claims_runs_subtree_and_completes_when_all_refined(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.page_memory.scope_enrichment_service import (
        execute_scope_enrichment,
    )

    del worker_contract_environment
    _install_fake_scope_pipeline(monkeypatch)

    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    storage = _InMemoryResultStorage(_build_storage_inputs())

    try:
        with Session(engine) as session:
            doc_id, job_id, revision_id = _seed_published_coarse_doc(
                session,
                suffix="ok",
                scope_states=[("p1-2", 1, 2, "queued"), ("p3-4", 3, 4, "refined")],
            )

        result = execute_scope_enrichment(
            job_result_id=revision_id,
            scope_id="p1-2",
            result_storage_factory=lambda: storage,
        )
        assert result["status"] == "refined"
        assert result["all_refined"] is True
        assert "images/page-1-fig-1.png" in storage.uploaded_raw
        assert "page_citation_assets/page-1.png" in storage.uploaded_raw
        assert storage.uploaded_zips == [f"results/{job_id}.zip"]

        with Session(engine) as session:
            db_chunks = list(
                session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == doc_id)
                    .where(DocumentChunk.job_result_id == revision_id)
                    .order_by(DocumentChunk.sort_order, DocumentChunk.id)
                ).all()
            )
            p1_2_chunks = [
                c
                for c in db_chunks
                if (c.chunk_metadata or {}).get("scope_id") == "p1-2"
            ]
            p3_4_chunks = [
                c
                for c in db_chunks
                if (c.chunk_metadata or {}).get("scope_id") == "p3-4"
            ]
            page_chunks = [c for c in p1_2_chunks if c.chunk_type == "page"]
            asset_chunks = [c for c in p1_2_chunks if c.chunk_type == "image"]
            assert [c.source_chunk_path for c in page_chunks] == [
                "doc.pdf/Parent/GrandA",
                "doc.pdf/Parent/GrandB",
            ]
            assert {
                (c.chunk_metadata or {}).get("enrichment_state")
                for c in page_chunks
            } == {"refined"}
            assert len(asset_chunks) == 1
            assert [c.chunk_id for c in p3_4_chunks] == ["coarse-p3-4"]

            row = session.execute(
                select(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == revision_id)
                .where(DocumentScopeEnrichment.scope_id == "p1-2")
            ).scalar_one()
            assert row.state == "refined"
            assert row.attempts == 1
            assert row.refined_at is not None

            job = session.execute(select(Job).where(Job.job_id == job_id)).scalar_one()
            assert (job.job_metadata or {}).get("enrichment_status") == "complete"
            job_result = session.execute(
                select(JobResult).where(JobResult.id == revision_id)
            ).scalar_one()
            assert job_result.result_s3_key == f"results/{job_id}.zip"
            assert (job_result.result_size or 0) > 0
            assert (job_result.inline_payload or {}).get("checksum") not in (
                None,
                "",
                "coarse",
            )

        second = execute_scope_enrichment(
            job_result_id=revision_id,
            scope_id="p1-2",
            result_storage_factory=lambda: storage,
        )
        assert second["status"] == "skipped"
    finally:
        engine.dispose()


def test_execute_scope_enrichment_aborts_when_revision_is_superseded(
    worker_contract_environment: None,
) -> None:
    from app.services.page_memory.scope_enrichment_service import (
        execute_scope_enrichment,
    )

    del worker_contract_environment

    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            doc_id, _job_id, revision_id = _seed_published_coarse_doc(
                session,
                suffix="stale",
                scope_states=[("p1-2", 1, 2, "queued")],
            )
            newer_job = Job(
                job_id="enrich-job-stale-newer",
                user_id="enrich-user-stale",
                job_type="document_ingestion",
                status="done",
                source_type="file",
                webhook_enabled=False,
            )
            session.add(newer_job)
            session.flush()
            newer = JobResult(
                id="enrich-rev-stale-newer",
                job_id=newer_job.job_id,
                document_id=doc_id,
                delivery_mode="url",
            )
            session.add(newer)
            session.flush()
            doc = session.execute(
                select(Document).where(Document.document_id == doc_id)
            ).scalar_one()
            doc.current_job_result_id = newer.id
            session.commit()

        called = False

        def fail_if_called(*args: object, **kwargs: object) -> None:
            nonlocal called
            called = True

        result = execute_scope_enrichment(
            job_result_id=revision_id,
            scope_id="p1-2",
            scope_publisher=fail_if_called,
        )
        assert result["status"] == "aborted"
        assert called is False

        with Session(engine) as session:
            row = session.execute(
                select(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == revision_id)
                .where(DocumentScopeEnrichment.scope_id == "p1-2")
            ).scalar_one()
            assert row.state == "failed"
    finally:
        engine.dispose()


def test_execute_scope_enrichment_reclaims_stale_running_scope(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from app.services.page_memory.scope_enrichment_service import (
        execute_scope_enrichment,
    )
    from shared.core.config import app_config
    from shared.utils.utc_now import utc_now_naive

    del worker_contract_environment
    _install_fake_scope_pipeline(monkeypatch)

    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    storage = _InMemoryResultStorage(_build_storage_inputs())
    published_calls: list[dict[str, Any]] = []

    def fake_scope_publisher(
        db: Session,
        *,
        scope: Any,
        scope_id: str,
        chunks: list[dict[str, Any]],
        section_summaries: dict[str, str],
    ) -> None:
        del db, scope, chunks, section_summaries
        published_calls.append({"scope_id": scope_id})

    try:
        with Session(engine) as session:
            _doc_id, _job_id, revision_id = _seed_published_coarse_doc(
                session,
                suffix="zombie",
                scope_states=[("p1-2", 1, 2, "running")],
            )
            # Simulate a crashed/OOM worker: the row is stuck in ``running`` with
            # a claim watermark older than the Celery hard time limit, so no live
            # task can still own it.
            stale_at = utc_now_naive() - timedelta(
                seconds=app_config.TASK_TIME_LIMIT_SECONDS + 60
            )
            session.execute(
                update(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == revision_id)
                .where(DocumentScopeEnrichment.scope_id == "p1-2")
                .values(updated_at=stale_at)
            )
            session.commit()

        result = execute_scope_enrichment(
            job_result_id=revision_id,
            scope_id="p1-2",
            result_storage_factory=lambda: storage,
            scope_publisher=fake_scope_publisher,
        )
        assert result["status"] == "refined"
        assert len(published_calls) == 1

        with Session(engine) as session:
            row = session.execute(
                select(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == revision_id)
                .where(DocumentScopeEnrichment.scope_id == "p1-2")
            ).scalar_one()
            assert row.state == "refined"
            assert row.attempts == 1
    finally:
        engine.dispose()


def test_execute_scope_enrichment_skips_fresh_running_scope(
    worker_contract_environment: None,
) -> None:
    from app.services.page_memory.scope_enrichment_service import (
        execute_scope_enrichment,
    )

    del worker_contract_environment

    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            _doc_id, _job_id, revision_id = _seed_published_coarse_doc(
                session,
                suffix="fresh-running",
                scope_states=[("p1-2", 1, 2, "running")],
            )

        called = False

        def fail_if_called(*args: object, **kwargs: object) -> None:
            nonlocal called
            called = True

        result = execute_scope_enrichment(
            job_result_id=revision_id,
            scope_id="p1-2",
            scope_publisher=fail_if_called,
        )
        assert result["status"] == "skipped"
        assert called is False

        with Session(engine) as session:
            row = session.execute(
                select(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == revision_id)
                .where(DocumentScopeEnrichment.scope_id == "p1-2")
            ).scalar_one()
            assert row.state == "running"
    finally:
        engine.dispose()


def test_zip_regeneration_failure_does_not_mark_job_complete(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.page_memory import scope_enrichment_service as ses_mod
    from app.services.page_memory.scope_enrichment_service import (
        execute_scope_enrichment,
    )

    del worker_contract_environment
    _install_fake_scope_pipeline(monkeypatch)
    monkeypatch.setattr(
        ses_mod,
        "_regenerate_and_upload_full_package",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("zip boom")),
    )

    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    storage = _InMemoryResultStorage(_build_storage_inputs())
    try:
        with Session(engine) as session:
            _doc_id, job_id, revision_id = _seed_published_coarse_doc(
                session,
                suffix="zip-fail",
                scope_states=[("p1-2", 1, 2, "queued")],
            )
        result = execute_scope_enrichment(
            job_result_id=revision_id,
            scope_id="p1-2",
            result_storage_factory=lambda: storage,
        )
        assert result["status"] == "refined"
        with Session(engine) as session:
            job = session.execute(select(Job).where(Job.job_id == job_id)).scalar_one()
            assert (job.job_metadata or {}).get("enrichment_status") == "coarse"
    finally:
        engine.dispose()


def test_enqueue_sends_scopes_low_priority_largest_first(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core import celery_app as celery_app_mod
    from shared.services.jobs.lifecycle.scope_enrichments import (
        ENRICH_SCOPE_TASK_NAME,
        LOW_ENRICHMENT_QUEUE,
        enqueue_low_priority_scope_enrichments_after_commit,
    )

    del worker_contract_environment
    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    sent: list[tuple[str, list[object], str | None]] = []

    class _FakeCelery:
        def send_task(
            self,
            name: str,
            args: list[object] | None = None,
            queue: str | None = None,
            **kwargs: object,
        ) -> None:
            del kwargs
            sent.append((name, list(args or []), queue))

    monkeypatch.setattr(celery_app_mod, "get_celery_app", lambda: _FakeCelery())
    try:
        with Session(engine) as session:
            _doc_id, _job_id, revision_id = _seed_published_coarse_doc(
                session,
                suffix="enq-order",
                scope_states=[
                    ("p1-2", 1, 2, "coarse"),
                    ("p3-10", 3, 10, "coarse"),
                ],
            )

        enqueue_low_priority_scope_enrichments_after_commit(revision_id)

        assert sent == [
            (ENRICH_SCOPE_TASK_NAME, [revision_id, "p3-10"], LOW_ENRICHMENT_QUEUE),
            (ENRICH_SCOPE_TASK_NAME, [revision_id, "p1-2"], LOW_ENRICHMENT_QUEUE),
        ]
        with Session(engine) as session:
            rows = {
                r.scope_id: (r.state, r.priority)
                for r in session.scalars(
                    select(DocumentScopeEnrichment).where(
                        DocumentScopeEnrichment.job_result_id == revision_id
                    )
                ).all()
            }
            assert rows == {"p1-2": ("queued", "low"), "p3-10": ("queued", "low")}
    finally:
        engine.dispose()


def test_sweeper_reenqueues_only_revisions_stuck_at_text(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from sqlalchemy import update

    from shared.core import celery_app as celery_app_mod
    from shared.services.jobs.lifecycle.scope_enrichments import (
        BUILD_STRUCTURE_TASK_NAME,
        sweep_progressive_enrichments_sync,
    )
    from shared.utils.utc_now import utc_now_naive

    del worker_contract_environment
    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    sent: list[tuple[str, list[object]]] = []

    class _FakeCelery:
        def send_task(
            self,
            name: str,
            args: list[object] | None = None,
            **kwargs: object,
        ) -> None:
            del kwargs
            sent.append((name, list(args or [])))

    monkeypatch.setattr(celery_app_mod, "get_celery_app", lambda: _FakeCelery())
    stale = utc_now_naive() - timedelta(hours=1)
    try:
        with Session(engine) as session:
            _doc, stuck_job, stuck_rev = _seed_published_coarse_doc(
                session,
                suffix="sweep-text",
                scope_states=[("p1-2", 1, 2, "refined")],
            )
            _doc, done_job, done_rev = _seed_published_coarse_doc(
                session,
                suffix="sweep-done",
                scope_states=[("p1-2", 1, 2, "refined")],
            )
            for job_id, status in ((stuck_job, "text"), (done_job, "complete")):
                job = session.execute(
                    select(Job).where(Job.job_id == job_id)
                ).scalar_one()
                job.job_metadata = {
                    **dict(job.job_metadata or {}),
                    "enrichment_status": status,
                }
            session.flush()
            session.execute(
                update(Job)
                .where(Job.job_id.in_([stuck_job, done_job]))
                .values(updated_at=stale)
            )
            session.commit()

        sweep_progressive_enrichments_sync()

        structure_sends = [args for name, args in sent if name == BUILD_STRUCTURE_TASK_NAME]
        assert [stuck_rev] in structure_sends
        assert [done_rev] not in structure_sends
    finally:
        engine.dispose()


def test_enqueue_failure_reverts_unsent_scopes_to_coarse_and_sweeper_retries_failed_scopes(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core import celery_app as celery_app_mod
    from shared.services.jobs.lifecycle.scope_enrichments import (
        enqueue_low_priority_scope_enrichments_after_commit,
        sweep_progressive_enrichments_sync,
    )

    del worker_contract_environment
    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)

    class _FailingCelery:
        def __init__(self) -> None:
            self.calls = 0

        def send_task(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("broker down")

    failing = _FailingCelery()
    monkeypatch.setattr(celery_app_mod, "get_celery_app", lambda: failing)

    try:
        with Session(engine) as session:
            _doc_id, _job_id, revision_id = _seed_published_coarse_doc(
                session,
                suffix="enq-revert",
                scope_states=[
                    ("p1-4", 1, 4, "coarse"),
                    ("p5-6", 5, 6, "coarse"),
                ],
            )

        sent = enqueue_low_priority_scope_enrichments_after_commit(revision_id)
        assert sent == ["p1-4"]

        with Session(engine) as session:
            rows = {
                r.scope_id: r
                for r in session.scalars(
                    select(DocumentScopeEnrichment).where(
                        DocumentScopeEnrichment.job_result_id == revision_id
                    )
                ).all()
            }
            assert rows["p1-4"].state == "queued"
            # Unsent p5-6 reverted to coarse instead of stuck in queued!
            assert rows["p5-6"].state == "coarse"
            assert rows["p5-6"].priority is None
            # Mark p1-4 as failed with attempts=1 (< MAX_SCOPE_ENRICHMENT_ATTEMPTS=3).
            rows["p1-4"].state = "failed"
            rows["p1-4"].attempts = 1
            session.commit()

        sent_by_sweep: list[tuple[str, str]] = []

        class _OkCelery:
            def send_task(
                self,
                name: str,
                args: list[object] | None = None,
                **kwargs: object,
            ) -> None:
                del name, kwargs
                if args and len(args) == 2:
                    sent_by_sweep.append((str(args[0]), str(args[1])))

        monkeypatch.setattr(celery_app_mod, "get_celery_app", lambda: _OkCelery())
        sweep_res = sweep_progressive_enrichments_sync()
        assert sweep_res["status"] == "success"
        retried_for_rev = [
            scope_id for rev, scope_id in sent_by_sweep if rev == revision_id
        ]
        assert set(retried_for_rev) == {"p1-4", "p5-6"}
    finally:
        engine.dispose()


def test_execute_structure_enrichment_upgrades_m0_text_to_coarse_and_enqueues_scopes(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from app.services.page_memory import scope_enrichment_service as ses_mod
    from app.services.page_memory.phase_a import PHASE_B_BLACKBOARD_REL
    from app.services.page_memory.scope_enrichment_service import (
        execute_structure_enrichment,
    )
    from shared.core import celery_app as celery_app_mod

    del worker_contract_environment

    database_url = make_url(get_contract_database_url()).set(
        drivername="postgresql+psycopg2"
    )
    engine = create_engine(database_url)
    sent_tasks: list[tuple[str, list[object]]] = []

    class _FakeCelery:
        def send_task(
            self,
            name: str,
            args: list[object] | None = None,
            **kwargs: object,
        ) -> None:
            del kwargs
            sent_tasks.append((name, list(args or [])))

    monkeypatch.setattr(celery_app_mod, "get_celery_app", lambda: _FakeCelery())

    skeletons = [
        SectionSkeleton("doc.pdf/Ch1", 1, 1, 2, "Ch1", "doc.pdf"),
        SectionSkeleton("doc.pdf/Ch2", 1, 3, 4, "Ch2", "doc.pdf"),
    ]
    anatomy = PageAnatomyMap(
        job_id="j-struct",
        file_path="source.pdf",
        page_count=4,
        page_features=[],
        page_labels=[],
        toc_result=TocResult(toc_pages=[]),
        shard_plan=ShardPlan(enabled=False, reason="not_needed"),
    )
    from types import SimpleNamespace

    fake_profile = SimpleNamespace(
        page_count=4,
        anatomy=anatomy,
        toc=SimpleNamespace(has_toc=True),
        page_full_text_cache={1: "p1 text", 2: "p2 text", 3: "p3 text", 4: "p4 text"},
        page_body_text_cache={1: "p1 body", 2: "p2 body", 3: "p3 body", 4: "p4 body"},
    )
    monkeypatch.setattr(
        ses_mod,
        "_resume_profile_from_m0_workspace",
        lambda **_kw: fake_profile,
    )
    import app.services.page_memory.skeleton_extractor as skel_mod

    monkeypatch.setattr(
        skel_mod, "extract_section_skeletons", lambda **_kw: skeletons
    )

    storage = _InMemoryResultStorage(
        {
            PHASE_B_SOURCE_PDF_REL: b"%PDF-1.4",
            PHASE_B_BLACKBOARD_REL: json.dumps({"page_count": 4}).encode("utf-8"),
            PHASE_B_FULL_TEXT_REL: json.dumps(
                {"1": "p1 text", "2": "p2 text", "3": "p3 text", "4": "p4 text"}
            ).encode("utf-8"),
            PHASE_B_BODY_TEXT_REL: json.dumps(
                {"1": "p1 body", "2": "p2 body", "3": "p3 body", "4": "p4 body"}
            ).encode("utf-8"),
        }
    )

    try:
        from sqlalchemy import text

        user_id = "struct-user"
        job_id = "struct-job"
        revision_id = "struct-rev"
        with Session(engine) as session:
            session.execute(
                text(
                    'INSERT INTO "user" (id, name, email) '
                    "VALUES (:id, :name, :email) ON CONFLICT (id) DO NOTHING"
                ),
                {"id": user_id, "name": "Struct", "email": "struct@test.local"},
            )
            job = Job(
                job_id=job_id,
                user_id=user_id,
                job_type="document_ingestion",
                status="done",
                source_type="file",
                webhook_enabled=False,
                job_metadata={
                    "namespace": "ns-struct",
                    "parse_track": "page_memory",
                    "ingestion_mode": "progressive",
                    "source_file_name": "doc.pdf",
                    "result_raw_prefix": f"results/{job_id}/",
                    "enrichment_status": "text",
                    "page_memory_phase_b": [
                        PHASE_B_SOURCE_PDF_REL,
                        PHASE_B_BLACKBOARD_REL,
                        PHASE_B_FULL_TEXT_REL,
                        PHASE_B_BODY_TEXT_REL,
                    ],
                },
            )
            session.add(job)
            session.flush()
            jr = JobResult(
                id=revision_id,
                job_id=job_id,
                delivery_mode="url",
                document_metadata={"enrichment_status": "text"},
                result_s3_key=f"results/{job_id}_m0.zip",
                result_size=10,
                inline_payload={"checksum": "m0"},
            )
            session.add(jr)
            session.flush()
            published = RetrievalPublicationService().publish_document_state(
                session,
                job_id=job_id,
                job_result_id=revision_id,
                chunks=[
                    {
                        "chunk_id": f"m0-p{p}",
                        "type": "page",
                        "content": f"p{p} text",
                        "path": f"doc.pdf/p{p}",
                        "metadata": {
                            "scope_id": f"p{p}-{p}",
                            "enrichment_state": "text",
                            "page_nums": [p],
                        },
                    }
                    for p in range(1, 5)
                ],
                update_namespace_snapshot=False,
            )
            assert published is not None and published.document_id is not None
            doc_id = published.document_id
            session.commit()

        result = execute_structure_enrichment(
            job_result_id=revision_id,
            result_storage_factory=lambda: storage,
        )
        assert result["status"] == "coarse"
        assert result["scope_count"] == 2
        assert PHASE_B_MANIFEST_REL in storage.uploaded_raw
        assert PHASE_B_ANATOMY_REL in storage.uploaded_raw
        assert len(sent_tasks) == 2

        with Session(engine) as session:
            chunks_after = list(
                session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == doc_id)
                    .where(DocumentChunk.job_result_id == revision_id)
                    .order_by(DocumentChunk.sort_order)
                ).all()
            )
            assert [c.source_chunk_path for c in chunks_after] == [
                "doc.pdf/Ch1",
                "doc.pdf/Ch2",
            ]
            assert {
                (c.chunk_metadata or {}).get("enrichment_state")
                for c in chunks_after
            } == {"coarse"}
            scope_rows = list(
                session.scalars(
                    select(DocumentScopeEnrichment)
                    .where(
                        DocumentScopeEnrichment.job_result_id == revision_id
                    )
                    .order_by(DocumentScopeEnrichment.scope_id)
                ).all()
            )
            assert [(r.scope_id, r.state) for r in scope_rows] == [
                ("p1-2", "queued"),
                ("p3-4", "queued"),
            ]
            job = session.execute(
                select(Job).where(Job.job_id == job_id)
            ).scalar_one()
            assert (job.job_metadata or {}).get("enrichment_status") == "coarse"
    finally:
        engine.dispose()


