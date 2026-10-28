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
    DocumentPageEmbedding,
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
        snapshot_id: str | None = None,
    ) -> Any:
        del result_dir, artifact_refs, snapshot_id
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
            "stages": {
                "token_usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                    "calls": 1,
                    "duration_ms": 20,
                    "by_task": {
                        "document_agent.coarse_profile": {
                            "prompt_tokens": 10,
                            "completion_tokens": 4,
                            "total_tokens": 14,
                            "calls": 1,
                            "duration_ms": 20,
                        }
                    },
                },
                "timing_ms": {"worker.parse.document": 1200},
            },
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
        from shared.services.ai.token_tracking import record_tokens

        record_tokens(
            {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
            model="test-tag-model",
            task="page_memory.tag",
            duration_ms=15,
        )
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


@pytest.mark.parametrize("failure_stage", ["none", "upload", "publication"])
@pytest.mark.parametrize("has_missing_vectors", [False, True])
def test_deferred_enrichment_keeps_baseline_until_final_package_is_durable(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    has_missing_vectors: bool,
) -> None:
    import zipfile
    import io
    import json

    from app.services.page_memory import scope_enrichment_service as service
    from shared.models.database.document import (
        DocumentMapUnit,
        DocumentPageEmbedding,
        RetrievalNamespaceGeneration,
    )
    from shared.models.schemas.progressive_ingestion import StoredSnapshot
    from shared.services.jobs.progressive_ingestion import (
        initialize_baseline_state,
        read_progressive_state,
        write_progressive_state,
    )
    from shared.services.retrieval.publication_scope_content import (
        replace_document_scope_content,
    )
    from shared.services.retrieval.publication_models import DocumentPublicationScope
    from shared.services.storage.result_storage import UploadedResultBundle

    del worker_contract_environment
    _install_fake_scope_pipeline(monkeypatch)
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    uploads: list[bytes] = []
    publications: list[str] = []
    invalidations: list[str] = []

    class Storage(_InMemoryResultStorage):
        def upload(
            self,
            *,
            job_id: str,
            result_dir: str,
            zip_file_path: str,
            artifact_refs: set[str] | None = None,
            snapshot_id: str | None = None,
        ) -> UploadedResultBundle:
            if failure_stage == "upload" and not uploads:
                uploads.append(b"failed")
                raise OSError("Interrupted final package upload")
            uploads.append(Path(zip_file_path).read_bytes())
            return super().upload(
                job_id=job_id,
                result_dir=result_dir,
                zip_file_path=zip_file_path,
                artifact_refs=artifact_refs,
                snapshot_id=snapshot_id,
            )

    def publish(
        db: Session,
        *,
        scope: DocumentPublicationScope,
        scope_id: str,
        chunks: list[dict[str, object]],
        section_summaries: dict[str, str] | None = None,
    ) -> dict[str, object]:
        publications.append(scope_id)
        manifest = replace_document_scope_content(
            db,
            scope=scope,
            scope_id=scope_id,
            chunks=chunks,
            section_summaries=section_summaries,
        )
        if failure_stage == "publication" and len(publications) == 1:
            raise RuntimeError("Interrupted database publication")
        return manifest

    from shared.services.retrieval import publication_scope_content as publication

    monkeypatch.setattr(
        publication,
        "bump_retrieval_namespace_cache_version_sync",
        lambda **kwargs: invalidations.append(kwargs["namespace"]),
    )
    monkeypatch.setattr(service, "_resolve_default_scope_publisher", lambda: publish)
    storage = Storage(_build_storage_inputs())
    try:
        with Session(engine) as db:
            document_id, job_id, revision_id = _seed_published_coarse_doc(
                db,
                suffix=f"deferred-{failure_stage}",
                scope_states=[("p1-2", 1, 2, "queued"), ("p3-4", 3, 4, "queued")],
            )
            result = db.get(JobResult, revision_id)
            state = initialize_baseline_state(
                snapshot=StoredSnapshot(
                    snapshot_id="baseline",
                    stage="baseline",
                    job_id=job_id,
                    job_result_id=revision_id,
                    document_id=document_id,
                    checksum="a" * 64,
                    archive_key=result.result_s3_key,
                ),
                missing_pages=[1] if has_missing_vectors else [],
            )
            state.stages["structure"]["status"] = "complete"
            result.document_metadata = write_progressive_state(
                result.document_metadata, state
            )
            db.commit()
            baseline_chunks = list(
                db.scalars(
                    select(DocumentChunk.id).where(
                        DocumentChunk.job_result_id == revision_id
                    )
                )
            )
            baseline_units = list(
                db.scalars(
                    select(DocumentMapUnit.id).where(
                        DocumentMapUnit.job_result_id == revision_id
                    )
                )
            )
            initial_generation: int = db.scalar(
                select(RetrievalNamespaceGeneration.generation).where(
                    RetrievalNamespaceGeneration.user_id
                    == f"enrich-user-deferred-{failure_stage}"
                )
            ) or 0

        first = service.execute_scope_enrichment(
            job_result_id=revision_id,
            scope_id="p1-2",
            result_storage_factory=lambda: storage,
        )
        assert first["status"] == "refined"
        assert first["all_refined"] is False
        assert publications == [], "A completed scope must not rebuild the live index"
        assert invalidations == []
        with Session(engine) as db:
            assert (
                list(
                    db.scalars(
                        select(DocumentChunk.id).where(
                            DocumentChunk.job_result_id == revision_id
                        )
                    )
                )
                == baseline_chunks
            )
            assert (
                list(
                    db.scalars(
                        select(DocumentMapUnit.id).where(
                            DocumentMapUnit.job_result_id == revision_id
                        )
                    )
                )
                == baseline_units
            )
            assert db.get(JobResult, revision_id).result_s3_key.endswith("_coarse.zip")

        if has_missing_vectors:
            from shared.services.ai import embedding
            import tempfile

            monkeypatch.setattr(
                embedding,
                "embed_images",
                lambda paths, **kwargs: [[0.1] * 3072 for _ in paths],
            )
            with Session(engine) as db:
                target = service._build_claimed_target(
                    document=db.get(Document, document_id),
                    job=db.get(Job, job_id),
                    job_metadata=dict(db.get(Job, job_id).job_metadata or {}),
                    source_file_name="doc.pdf",
                )
            with tempfile.TemporaryDirectory() as workspace:
                service._repair_missing_page_embeddings(
                    job_result_id=revision_id,
                    target=target,
                    storage=storage,
                    workspace_dir=workspace,
                )
            with Session(engine) as db:
                assert (
                    list(
                        db.scalars(
                            select(DocumentPageEmbedding.id).where(
                                DocumentPageEmbedding.job_result_id == revision_id
                            )
                        )
                    )
                    == []
                ), "Repaired vectors must remain staged until final publication"
            assert invalidations == []

        second = service.execute_scope_enrichment(
            job_result_id=revision_id,
            scope_id="p3-4",
            result_storage_factory=lambda: storage,
        )
        assert second["all_refined"] is True
        if failure_stage != "none":
            with Session(engine) as db:
                assert (
                    list(
                        db.scalars(
                            select(DocumentChunk.id).where(
                                DocumentChunk.job_result_id == revision_id
                            )
                        )
                    )
                    == baseline_chunks
                )
                assert (
                    read_progressive_state(
                        db.get(JobResult, revision_id).document_metadata
                    ).current_snapshot_id
                    == "baseline"
                )
                target = service._build_claimed_target(
                    document=db.get(Document, document_id),
                    job=db.get(Job, job_id),
                    job_metadata=dict(db.get(Job, job_id).job_metadata or {}),
                    source_file_name="doc.pdf",
                )
                state = read_progressive_state(
                    db.get(JobResult, revision_id).document_metadata
                )
                state.stages["finalization"]["next_retry_at"] = None
                db.get(
                    JobResult, revision_id
                ).document_metadata = write_progressive_state(
                    db.get(JobResult, revision_id).document_metadata, state
                )
                db.commit()
            assert publications == (["*"] if failure_stage == "publication" else [])
            assert invalidations == []
            import tempfile

            with tempfile.TemporaryDirectory() as workspace:
                service._finalize_all_scopes_refined(
                    job_result_id=revision_id,
                    target=target,
                    storage=storage,
                    workspace_dir=workspace,
                )

        assert publications == (["*", "*"] if failure_stage == "publication" else ["*"])
        assert len(invalidations) == 1
        with Session(engine) as db:
            rows = list(
                db.scalars(
                    select(DocumentChunk).where(
                        DocumentChunk.job_result_id == revision_id
                    )
                )
            )
            assert not any(row.chunk_id.startswith("coarse-") for row in rows)
            assert {row.chunk_metadata["scope_id"] for row in rows} == {"p1-2", "p3-4"}
            assert {
                row.chunk_metadata["enrichment_state"]
                for row in rows
                if row.chunk_type == "page"
            } == {"refined"}
            state = read_progressive_state(
                db.get(JobResult, revision_id).document_metadata
            )
            assert state.enhancement.status == "complete"
            assert [snapshot.stage for snapshot in state.snapshots] == [
                "baseline",
                "final",
            ]
            assert db.get(Job, job_id).status == "done"
            assert db.scalar(
                select(RetrievalNamespaceGeneration.generation).where(
                    RetrievalNamespaceGeneration.user_id
                    == f"enrich-user-deferred-{failure_stage}"
                )
            ) == initial_generation + 1
            if has_missing_vectors:
                vector = db.scalars(
                    select(DocumentPageEmbedding).where(
                        DocumentPageEmbedding.job_result_id == revision_id
                    )
                ).one()
                assert vector.page_num == 1
                assert vector.chunk_id in {row.chunk_id for row in rows}
            with zipfile.ZipFile(io.BytesIO(uploads[-1])) as archive:
                chunks_file = next(
                    name for name in archive.namelist() if name.endswith("chunks.json")
                )
                archived = json.loads(archive.read(chunks_file))
                assert {chunk["chunk_id"] for chunk in archived["chunks"]} == {
                    row.chunk_id for row in rows
                }
    finally:
        engine.dispose()


def test_deferred_structure_schedules_staged_ranges_without_republishing_baseline(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta
    from app.services.page_memory import scope_enrichment_service as service
    from shared.models.schemas.progressive_ingestion import StoredSnapshot
    from shared.services.jobs.progressive_ingestion import (
        initialize_baseline_state,
        write_progressive_state,
    )
    from shared.services.jobs.lifecycle.scope_enrichments import (
        sweep_progressive_enrichments_sync,
    )
    from shared.utils.utc_now import utc_now_naive
    from shared.core import celery_app as celery_module

    del worker_contract_environment

    class Broker:
        def send_task(self, *args: object, **kwargs: object) -> None:
            pass

    monkeypatch.setattr(celery_module, "get_celery_app", lambda: Broker())
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    publications: list[str] = []
    try:
        with Session(engine) as db:
            document_id, job_id, revision_id = _seed_published_coarse_doc(
                db,
                suffix="staged-structure",
                scope_states=[("p1-2", 1, 2, "coarse"), ("p3-4", 3, 4, "coarse")],
            )
            result = db.get(JobResult, revision_id)
            state = initialize_baseline_state(
                snapshot=StoredSnapshot(
                    snapshot_id="baseline",
                    stage="baseline",
                    job_id=job_id,
                    job_result_id=revision_id,
                    document_id=document_id,
                    checksum="a" * 64,
                    archive_key=result.result_s3_key,
                ),
                missing_pages=[],
            )
            state.stages["structure"].update(
                status="running", claim_generation="structure"
            )
            result.document_metadata = {
                **write_progressive_state(result.document_metadata, state),
                "enrichment_status": "text",
                "structure_claim_generation": "structure",
                "structure_claimed_at": (
                    utc_now_naive() - timedelta(hours=2)
                ).isoformat(),
            }
            db.commit()
            baseline = list(
                db.scalars(
                    select(DocumentChunk.id).where(
                        DocumentChunk.job_result_id == revision_id
                    )
                )
            )
            target = service._build_claimed_target(
                document=db.get(Document, document_id),
                job=db.get(Job, job_id),
                job_metadata=dict(db.get(Job, job_id).job_metadata or {}),
                source_file_name="doc.pdf",
                claim_generation="structure",
                result_metadata=result.document_metadata,
            )
        has_published_structure: bool = service._publish_coarse_structure_and_write_scopes(
            job_result_id=revision_id,
            target=target,
            publication_scope=service._build_publication_scope(
                target=target, job_result_id=revision_id
            ),
            chunks=[
                {
                    "chunk_id": "staged-scope",
                    "type": "page",
                    "content": "private coarse text",
                    "path": "doc.pdf/Chapter",
                    "metadata": {"scope_id": "p1-4"},
                }
            ],
            section_summaries={},
            phase_b_refs=[],
            scope_publisher=lambda *args, **kwargs: publications.append(
                kwargs["scope_id"]
            ),
        )
        assert has_published_structure
        assert publications == []
        with Session(engine) as db:
            assert (
                list(
                    db.scalars(
                        select(DocumentChunk.id).where(
                            DocumentChunk.job_result_id == revision_id
                        )
                    )
                )
                == baseline
            )
            assert list(
                db.scalars(
                    select(DocumentScopeEnrichment.scope_id).where(
                        DocumentScopeEnrichment.job_result_id == revision_id
                    )
                )
            ) == ["p1-4"]
        swept = sweep_progressive_enrichments_sync(structure_timeout_seconds=0)
        assert revision_id not in swept["reenqueued_structures"], (
            "Completed staging must not be treated as an unpublished structure"
        )
    finally:
        engine.dispose()


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
    assert celery_app.conf.task_routes[
        "app.core.tasks.enrichment_tasks.build_page_memory_structure"
    ] == {"queue": "document_ingestion_medium"}
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
        assert any(
            reference.startswith("images/") and reference.endswith("-page-1-fig-1.png")
            for reference in storage.uploaded_raw
        )
        assert any(
            reference.startswith("page_citation_assets/")
            and reference.endswith("-page-1.png")
            for reference in storage.uploaded_raw
        )
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
                (c.chunk_metadata or {}).get("enrichment_state") for c in page_chunks
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
            usage = ((job.job_metadata or {}).get("stages") or {}).get("token_usage") or {}
            assert usage.get("total_tokens") == 24
            assert usage.get("calls") == 2
            assert usage.get("duration_ms") == 35
            assert usage.get("by_task", {}).get("document_agent.coarse_profile", {}).get(
                "calls"
            ) == 1
            assert usage.get("by_task", {}).get("page_memory.tag", {}).get(
                "total_tokens"
            ) == 10
            assert ((job.job_metadata or {}).get("stages") or {}).get("timing_ms", {}).get(
                "worker.parse.document"
            ) == 1200
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
        with Session(engine) as session:
            job = session.execute(select(Job).where(Job.job_id == job_id)).scalar_one()
            usage = ((job.job_metadata or {}).get("stages") or {}).get("token_usage") or {}
            assert usage.get("total_tokens") == 24
            assert usage.get("calls") == 2
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

        structure_sends = [
            args for name, args in sent if name == BUILD_STRUCTURE_TASK_NAME
        ]
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
    from shared.services.jobs.lifecycle.scope_enrichments import (
        sweep_progressive_enrichments_sync,
    )

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
    call_order: list[str] = []

    def _resume_and_record(**_kw: object) -> object:
        from shared.services.ai.token_tracking import record_tokens

        call_order.append("resume_profile")
        record_tokens(
            {"prompt_tokens": 6, "completion_tokens": 1, "total_tokens": 7},
            model="test-toc-model",
            task="document_agent.toc_anchor_confirm",
            duration_ms=11,
        )
        return fake_profile

    monkeypatch.setattr(
        ses_mod,
        "_resume_profile_from_m0_workspace",
        _resume_and_record,
    )
    import app.services.page_memory.page_renderer as page_renderer_mod
    import app.services.page_memory.skeleton_extractor as skel_mod
    from shared.core.config import settings
    from shared.services.ai import embedding as embedding_pkg
    from shared.services.ai.embedding import DEFAULT_EMBEDDING_DIM

    monkeypatch.setattr(
        skel_mod, "extract_section_skeletons", lambda **_kw: skeletons
    )
    monkeypatch.setattr(settings, "VISUAL_RETRIEVAL_ENABLED", True)

    def _fake_defer_render(
        *,
        output_dir: str,
        scope_id: str,
        pages: list[int],
        page_texts: dict[int, str],
        **_kw: object,
    ) -> list[PageRenderResult]:
        call_order.append(f"render:{scope_id}")
        render_dir = Path(output_dir) / "pages" / scope_id
        render_dir.mkdir(parents=True, exist_ok=True)
        out: list[PageRenderResult] = []
        for page in pages:
            img_path = render_dir / f"page-{page}.png"
            img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([page]))
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

    vec_one = [1.0] + [0.0] * (DEFAULT_EMBEDDING_DIM - 1)
    monkeypatch.setattr(
        page_renderer_mod, "render_document_pages", _fake_defer_render
    )
    monkeypatch.setattr(
        embedding_pkg,
        "embed_images",
        lambda inputs, **_kw: (
            call_order.append("embed_images") or [vec_one for _ in inputs]
        ),
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
                    "stages": {
                        "token_usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 4,
                            "total_tokens": 14,
                            "calls": 1,
                            "duration_ms": 20,
                            "by_task": {
                                "document_agent.coarse_profile": {
                                    "prompt_tokens": 10,
                                    "completion_tokens": 4,
                                    "total_tokens": 14,
                                    "calls": 1,
                                    "duration_ms": 20,
                                }
                            },
                        },
                        "timing_ms": {"worker.parse.document": 800},
                    },
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
        assert call_order == ["render:m0_defer", "embed_images", "resume_profile"]
        assert PHASE_B_MANIFEST_REL not in storage.uploaded_raw
        assert any(
            reference.startswith("phase_b/") and reference.endswith("/manifest.json")
            for reference in storage.uploaded_raw
        )
        assert any(
            reference.startswith("phase_b/") and reference.endswith("/anatomy.json")
            for reference in storage.uploaded_raw
        )
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
                (c.chunk_metadata or {}).get("enrichment_state") for c in chunks_after
            } == {"coarse"}
            emb_rows = list(
                session.scalars(
                    select(DocumentPageEmbedding)
                    .where(DocumentPageEmbedding.document_id == doc_id)
                    .where(DocumentPageEmbedding.job_result_id == revision_id)
                    .order_by(DocumentPageEmbedding.page_num)
                ).all()
            )
            assert [r.page_num for r in emb_rows] == [1, 2, 3, 4]
            assert [r.chunk_id for r in emb_rows] == [
                chunks_after[0].chunk_id,
                chunks_after[0].chunk_id,
                chunks_after[1].chunk_id,
                chunks_after[1].chunk_id,
            ]
            scope_rows = list(
                session.scalars(
                    select(DocumentScopeEnrichment)
                    .where(DocumentScopeEnrichment.job_result_id == revision_id)
                    .order_by(DocumentScopeEnrichment.scope_id)
                ).all()
            )
            assert [(r.scope_id, r.state) for r in scope_rows] == [
                ("p1-2", "queued"),
                ("p3-4", "queued"),
            ]
            job = session.execute(select(Job).where(Job.job_id == job_id)).scalar_one()
            assert (job.job_metadata or {}).get("enrichment_status") == "coarse"
            usage = ((job.job_metadata or {}).get("stages") or {}).get("token_usage") or {}
            assert usage.get("total_tokens") == 21
            assert usage.get("calls") == 2
            assert usage.get("duration_ms") == 31
            assert usage.get("by_task", {}).get("document_agent.toc_anchor_confirm", {}).get(
                "total_tokens"
            ) == 7
            assert ((job.job_metadata or {}).get("stages") or {}).get("timing_ms", {}).get(
                "worker.parse.document"
            ) == 800

        # Reset to text while structure_claimed_at is still fresh: both a duplicate
        # structure task and sweep must skip it rather than re-running or re-enqueuing.
        with Session(engine) as session:
            job = session.execute(select(Job).where(Job.job_id == job_id)).scalar_one()
            jr = session.execute(
                select(JobResult).where(JobResult.id == revision_id)
            ).scalar_one()
            job.job_metadata = {
                **dict(job.job_metadata or {}),
                "enrichment_status": "text",
            }
            jr.document_metadata = {
                **dict(jr.document_metadata or {}),
                "enrichment_status": "text",
            }
            session.commit()

        dup_res = execute_structure_enrichment(
            job_result_id=revision_id,
            result_storage_factory=lambda: storage,
        )
        assert dup_res["status"] == "skipped"
        sweep_while_claimed = sweep_progressive_enrichments_sync(
            structure_timeout_seconds=0
        )
        assert revision_id not in sweep_while_claimed["reenqueued_structures"]
    finally:
        engine.dispose()


def test_sweeper_recovers_committed_queued_scope_without_broker_delivery(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core import celery_app as celery_app_mod
    from shared.services.jobs.lifecycle.scope_enrichments import (
        sweep_progressive_enrichments_sync,
    )

    del worker_contract_environment
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    sent: list[list[object]] = []

    class Broker:
        def send_task(self, name: str, args: list[object], **kwargs: object) -> None:
            del name, kwargs
            sent.append(args)

    monkeypatch.setattr(celery_app_mod, "get_celery_app", lambda: Broker())
    try:
        with Session(engine) as session:
            _, _, revision = _seed_published_coarse_doc(
                session, suffix="queued-gap", scope_states=[("p1-2", 1, 2, "queued")]
            )
        sweep_progressive_enrichments_sync()
        assert [revision, "p1-2"] in sent
    finally:
        engine.dispose()


def test_late_scope_failure_cannot_override_recovered_claim(
    worker_contract_environment: None,
) -> None:
    from app.services.page_memory.scope_enrichment_service import (
        _claim_and_validate_scope,
        _mark_scope_failed,
    )
    from datetime import timedelta
    from shared.utils.utc_now import utc_now_naive

    del worker_contract_environment
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    try:
        with Session(engine) as session:
            _, _, revision = _seed_published_coarse_doc(
                session, suffix="late-failure", scope_states=[("p1-2", 1, 2, "queued")]
            )
        _, first = _claim_and_validate_scope(job_result_id=revision, scope_id="p1-2")
        assert first is not None
        with Session(engine) as session:
            session.execute(
                update(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == revision)
                .values(updated_at=utc_now_naive() - timedelta(hours=2))
            )
            session.commit()
        _, second = _claim_and_validate_scope(job_result_id=revision, scope_id="p1-2")
        assert second is not None
        assert first.claim_generation != second.claim_generation
        _mark_scope_failed(
            job_result_id=revision,
            scope_id="p1-2",
            claim_generation=first.claim_generation,
        )
        with Session(engine) as session:
            row = session.scalars(
                select(DocumentScopeEnrichment).where(
                    DocumentScopeEnrichment.job_result_id == revision
                )
            ).one()
            assert row.state == "running"
            assert row.claim_generation == second.claim_generation
    finally:
        engine.dispose()


def test_final_upload_failure_recovers_without_reparsing_completed_scopes(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.page_memory import scope_enrichment_service as service
    from shared.models.schemas.progressive_ingestion import StoredSnapshot
    from shared.services.jobs.progressive_ingestion import (
        initialize_baseline_state,
        write_progressive_state,
        read_progressive_state,
    )

    del worker_contract_environment
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    storage = _InMemoryResultStorage(_build_storage_inputs())
    try:
        with Session(engine) as db:
            doc_id, job_id, revision = _seed_published_coarse_doc(
                db,
                suffix="recover-final-upload",
                scope_states=[("p1-2", 1, 2, "refined")],
            )
            result = db.get(JobResult, revision)
            state = initialize_baseline_state(
                snapshot=StoredSnapshot(
                    snapshot_id="baseline",
                    stage="baseline",
                    job_id=job_id,
                    job_result_id=revision,
                    document_id=doc_id,
                    checksum="a" * 64,
                    archive_key="immutable/baseline.zip",
                ),
                missing_pages=[],
            )
            # An in-flight revision that already published its scopes retains
            # the legacy protocol; new revisions are covered by deferred tests.
            state.publication_mode = "incremental"
            state.stages["structure"]["status"] = "complete"
            result.document_metadata = write_progressive_state(
                result.document_metadata, state
            )
            db.commit()
            target = service._build_claimed_target(
                document=db.get(Document, doc_id),
                job=db.get(Job, job_id),
                job_metadata=dict(db.get(Job, job_id).job_metadata or {}),
                source_file_name="doc.pdf",
            )
        calls: list[str] = []

        def package(**kwargs: object):
            calls.append("upload")
            if len(calls) == 1:
                raise RuntimeError("object storage interrupted")
            return StoredSnapshot(
                snapshot_id="final",
                stage="final",
                job_id=job_id,
                job_result_id=revision,
                document_id=doc_id,
                checksum="b" * 64,
                archive_key="immutable/final.zip",
            ), 10

        monkeypatch.setattr(service, "_regenerate_and_upload_full_package", package)
        service._finalize_all_scopes_refined(
            job_result_id=revision, target=target, storage=storage, workspace_dir="/tmp"
        )
        with Session(engine) as db:
            state = read_progressive_state(
                db.get(JobResult, revision).document_metadata
            )
            assert state.stages["finalization"]["status"] == "failed"
            assert state.current_snapshot_id == "baseline"
        service._finalize_all_scopes_refined(
            job_result_id=revision, target=target, storage=storage, workspace_dir="/tmp"
        )
        service._finalize_all_scopes_refined(
            job_result_id=revision, target=target, storage=storage, workspace_dir="/tmp"
        )
        with Session(engine) as db:
            state = read_progressive_state(
                db.get(JobResult, revision).document_metadata
            )
            assert state.enhancement.status == "complete"
            assert state.current_snapshot_id == "final"
            assert [snapshot.archive_key for snapshot in state.snapshots] == [
                "immutable/baseline.zip",
                "immutable/final.zip",
            ]
            assert state.stages["finalization"]["attempts"] == 2
            assert len(calls) == 2
            assert (
                db.scalar(
                    select(DocumentScopeEnrichment.attempts).where(
                        DocumentScopeEnrichment.job_result_id == revision
                    )
                )
                == 0
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("has_deferred_publication", [False, True])
@pytest.mark.parametrize("has_initial_vectors", [True, False])
def test_vector_repair_retries_only_remaining_pages_and_keeps_successful_vectors(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    has_deferred_publication: bool,
    has_initial_vectors: bool,
) -> None:
    from types import SimpleNamespace
    from app.services.page_memory import (
        scope_enrichment_service as service,
        page_renderer,
    )
    from shared.services.ai import embedding
    from shared.models.schemas.progressive_ingestion import StoredSnapshot
    from shared.models.database.document import DocumentPageEmbedding
    from shared.services.jobs.progressive_ingestion import (
        initialize_baseline_state,
        write_progressive_state,
        read_progressive_state,
    )

    del worker_contract_environment
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    storage = _InMemoryResultStorage(_build_storage_inputs())
    invalidations: list[str] = []
    from shared.services.retrieval import publication_scope_content

    monkeypatch.setattr(
        publication_scope_content,
        "bump_retrieval_namespace_cache_version_sync",
        lambda **kwargs: invalidations.append(kwargs["namespace"]),
    )
    rendered_pages: list[list[int]] = []

    def render(**kwargs: object):
        pages = list(kwargs["pages"])
        rendered_pages.append(pages)
        return [
            SimpleNamespace(page_index=page, image_path=f"page-{page}.jpg")
            for page in pages
        ]

    monkeypatch.setattr(page_renderer, "render_document_pages", render)
    vectors: list[list[list[float] | None]] = [
        ([[0.25] * 3072] if has_initial_vectors else [[0.25] * 3072, [0.25] * 3072]) + [None],
        [[0.5] * 3072],
    ]
    monkeypatch.setattr(
        embedding, "embed_images", lambda *args, **kwargs: vectors.pop(0)
    )
    try:
        with Session(engine) as db:
            doc_id, job_id, revision = _seed_published_coarse_doc(
                db, suffix="repair-partial", scope_states=[("p1-3", 1, 3, "refined")]
            )
            result = db.get(JobResult, revision)
            state = initialize_baseline_state(
                snapshot=StoredSnapshot(
                    snapshot_id="baseline",
                    stage="baseline",
                    job_id=job_id,
                    job_result_id=revision,
                    document_id=doc_id,
                    checksum="a" * 64,
                    archive_key="baseline.zip",
                ),
                missing_pages=[2, 3] if has_initial_vectors else [1, 2, 3],
            )
            if not has_deferred_publication:
                state.publication_mode = "incremental"
            state.stages["structure"]["status"] = "complete"
            result.document_metadata = write_progressive_state(
                result.document_metadata, state
            )
            chunk = db.scalar(
                select(DocumentChunk).where(DocumentChunk.document_id == doc_id)
            )
            chunk.chunk_metadata = {**chunk.chunk_metadata, "page_nums": [1, 2, 3]}
            owner = chunk.chunk_id
            if has_initial_vectors:
                db.add(
                    DocumentPageEmbedding(
                        id="existing-page-one",
                        user_id="enrich-user-repair-partial",
                        namespace="ns-repair-partial",
                        document_id=doc_id,
                        job_result_id=revision,
                        chunk_id=owner,
                        page_num=1,
                        modality="visual",
                        model="test",
                        dims=3072,
                        embedding=[0.1] * 3072,
                    )
                )
            db.commit()
            target = service._build_claimed_target(
                document=db.get(Document, doc_id),
                job=db.get(Job, job_id),
                job_metadata=dict(db.get(Job, job_id).job_metadata or {}),
                source_file_name="doc.pdf",
            )
        for attempt in range(2):
            service._repair_missing_page_embeddings(
                job_result_id=revision,
                target=target,
                storage=storage,
                workspace_dir=str(tmp_path),
            )
            if attempt == 0:
                service._finalize_all_scopes_refined(
                    job_result_id=revision, target=target, storage=storage,
                    workspace_dir=str(tmp_path),
                )
                assert storage.uploaded_zips == [], "Missing required vectors must withhold final publication"
        with Session(engine) as db:
            rows = db.scalars(
                select(DocumentPageEmbedding)
                .where(DocumentPageEmbedding.job_result_id == revision)
                .order_by(DocumentPageEmbedding.page_num)
            ).all()
            assert [row.page_num for row in rows] == (
                ([1] if has_initial_vectors else []) if has_deferred_publication else [1, 2, 3]
            )
            if has_initial_vectors:
                assert rows[0].id == "existing-page-one"
            if has_deferred_publication:
                from app.services.page_memory.staged_enrichment import StagedEnrichment
                references = db.get(JobResult, revision).document_metadata["page_memory_staged_vectors"]
                repaired: dict[int, list[float]] = {}
                for reference in references:
                    output = StagedEnrichment.download(
                        storage=storage, job_id=job_id, raw_prefix=target.raw_prefix,
                        reference=reference, workspace_dir=str(tmp_path),
                        job_result_id=revision, scope_id="vector-repair",
                    )
                    repaired.update(output.embeddings)
                assert set(repaired) == ({2, 3} if has_initial_vectors else {1, 2, 3})
            assert (
                read_progressive_state(
                    db.get(JobResult, revision).document_metadata
                ).stages["vector_repair"]["status"]
                == "complete"
            )
        assert rendered_pages == [([2, 3] if has_initial_vectors else [1, 2, 3]), [3]]
        assert invalidations == ([] if has_deferred_publication else [target.namespace, target.namespace])
    finally:
        engine.dispose()


def test_sweeper_stops_reclaiming_an_expired_third_scope_attempt(
    worker_contract_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta
    from shared.core import celery_app as celery_app_module
    from shared.services.jobs.lifecycle.scope_enrichments import (
        sweep_progressive_enrichments_sync,
    )
    from shared.utils.utc_now import utc_now_naive

    del worker_contract_environment
    sent: list[object] = []

    class Broker:
        def send_task(self, *args: object, **kwargs: object) -> None:
            sent.append(kwargs.get("args"))

    monkeypatch.setattr(celery_app_module, "get_celery_app", lambda: Broker())
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    try:
        with Session(engine) as db:
            _, _, revision = _seed_published_coarse_doc(
                db, suffix="bounded-scope", scope_states=[("p1-2", 1, 2, "running")]
            )
            db.execute(
                update(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == revision)
                .values(attempts=3, updated_at=utc_now_naive() - timedelta(hours=2))
            )
            db.commit()
        sweep_progressive_enrichments_sync()
        with Session(engine) as db:
            row = db.scalars(
                select(DocumentScopeEnrichment).where(
                    DocumentScopeEnrichment.job_result_id == revision
                )
            ).one()
            assert row.state == "failed"
        assert [revision, "p1-2"] not in sent
    finally:
        engine.dispose()


def test_late_scope_publication_does_not_overwrite_a_recovered_claim(
    worker_contract_environment: None,
) -> None:
    from datetime import timedelta
    import pandas as pd
    from app.services.page_memory import scope_enrichment_service as service
    from shared.utils.utc_now import utc_now_naive

    del worker_contract_environment
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    try:
        with Session(engine) as db:
            _, _, revision = _seed_published_coarse_doc(
                db, suffix="stale-publisher", scope_states=[("p1-2", 1, 2, "queued")]
            )
        _, first = service._claim_and_validate_scope(
            job_result_id=revision, scope_id="p1-2"
        )
        with Session(engine) as db:
            db.execute(
                update(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == revision)
                .values(updated_at=utc_now_naive() - timedelta(hours=2))
            )
            db.commit()
        _, second = service._claim_and_validate_scope(
            job_result_id=revision, scope_id="p1-2"
        )
        called: list[bool] = []
        result = service._publish_and_mark_refined(
            job_result_id=revision,
            scope_id="p1-2",
            target=first,
            output=service.ScopeRefinementOutput(
                scope_id="p1-2",
                dataframe=pd.DataFrame(),
                chunks=[],
                section_summaries={},
                artifact_refs=set(),
            ),
            scope_publisher=lambda *args, **kwargs: called.append(True),
        )
        assert result is None
        assert called == []
        with Session(engine) as db:
            row = db.scalars(
                select(DocumentScopeEnrichment).where(
                    DocumentScopeEnrichment.job_result_id == revision
                )
            ).one()
            assert row.state == "running"
            assert row.claim_generation == second.claim_generation
    finally:
        engine.dispose()


def test_structure_publication_rejects_an_exhausted_stage_generation(
    worker_contract_environment: None,
) -> None:
    from app.services.page_memory import scope_enrichment_service as service
    from shared.models.schemas.progressive_ingestion import (
        StoredSnapshot,
        EnhancementProgress,
    )
    from shared.services.jobs.progressive_ingestion import (
        initialize_baseline_state,
        write_progressive_state,
    )

    del worker_contract_environment
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    try:
        with Session(engine) as db:
            doc_id, job_id, revision = _seed_published_coarse_doc(
                db,
                suffix="exhausted-structure",
                scope_states=[("p1-2", 1, 2, "queued")],
            )
            state = initialize_baseline_state(
                snapshot=StoredSnapshot(
                    snapshot_id="baseline",
                    stage="baseline",
                    job_id=job_id,
                    job_result_id=revision,
                    document_id=doc_id,
                    checksum="a" * 64,
                    archive_key="baseline.zip",
                ),
                missing_pages=[],
            )
            state.stages["structure"] = {
                "status": "failed",
                "attempts": 3,
                "claim_generation": None,
            }
            state.enhancement = EnhancementProgress(status="failed", can_retry=True)
            result = db.get(JobResult, revision)
            result.document_metadata = write_progressive_state(
                {"structure_claim_generation": "old"}, state
            )
            db.commit()
            target = service._build_claimed_target(
                document=db.get(Document, doc_id),
                job=db.get(Job, job_id),
                job_metadata=dict(db.get(Job, job_id).job_metadata or {}),
                source_file_name="doc.pdf",
                claim_generation="old",
            )
        called: list[bool] = []
        published = service._publish_coarse_structure_and_write_scopes(
            job_result_id=revision,
            target=target,
            publication_scope=service._build_publication_scope(
                target=target, job_result_id=revision
            ),
            chunks=[],
            section_summaries={},
            phase_b_refs=[],
            scope_publisher=lambda *args, **kwargs: called.append(True),
        )
        assert published is False
        assert called == []
    finally:
        engine.dispose()


def test_recovery_cannot_overwrite_a_concurrent_manual_retry(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event, current_thread
    from sqlalchemy import event
    from app.services.page_memory import scope_enrichment_service as service
    from shared.models.schemas.progressive_ingestion import StoredSnapshot
    from shared.services.jobs.progressive_ingestion import (
        initialize_baseline_state, read_progressive_state, write_progressive_state,
    )
    from shared.services.jobs.progressive_retry import renew_failed_enhancement

    del worker_contract_environment
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    has_scope_read: Event = Event()
    has_retry_started: Event = Event()
    has_retry_finished: Event = Event()
    try:
        with Session(engine) as db:
            document_id, job_id, revision_id = _seed_published_coarse_doc(
                db, suffix="recovery-retry-race", scope_states=[("p1-2", 1, 2, "failed")],
            )
            revision = db.get(JobResult, revision_id)
            assert revision is not None
            state = initialize_baseline_state(
                snapshot=StoredSnapshot(
                    snapshot_id="baseline", stage="baseline", job_id=job_id,
                    job_result_id=revision_id, document_id=document_id,
                    checksum="a" * 64, archive_key="baseline.zip",
                ), missing_pages=[],
            )
            state.enhancement.status = "running"
            revision.document_metadata = write_progressive_state({}, state)
            db.execute(update(DocumentScopeEnrichment).where(
                DocumentScopeEnrichment.job_result_id == revision_id
            ).values(attempts=3))
            db.commit()

        @contextmanager
        def database():
            with Session(engine) as db:
                yield db
                db.commit()

        def pause_scope_read(connection, cursor, statement, parameters, context, many):
            del connection, cursor, parameters, context, many
            if (
                current_thread().name.startswith("recovery-race")
                and statement.startswith("SELECT document_scope_enrichments.state")
            ):
                has_scope_read.set()
                assert has_retry_started.wait(5)
                # A guarded recovery keeps the retry blocked until its commit.
                has_retry_finished.wait(0.5)

        event.listen(engine, "after_cursor_execute", pause_scope_read)
        monkeypatch.setattr(service, "get_sync_db_context", database)

        def retry() -> None:
            assert has_scope_read.wait(5)
            has_retry_started.set()
            with Session(engine) as db:
                db.execute(select(Document).where(
                    Document.document_id == document_id
                ).with_for_update()).scalar_one()
                revision = db.execute(select(JobResult).where(
                    JobResult.id == revision_id
                ).with_for_update(of=JobResult)).scalar_one()
                state = read_progressive_state(revision.document_metadata)
                assert state is not None
                state.enhancement.status = "failed"
                state.enhancement.can_retry = True
                renewed = renew_failed_enhancement(state, job_id=job_id)
                revision.document_metadata = write_progressive_state({}, renewed)
                db.execute(update(DocumentScopeEnrichment).where(
                    DocumentScopeEnrichment.job_result_id == revision_id
                ).values(state="coarse", attempts=0, claim_generation=None))
                db.commit()
            has_retry_finished.set()

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="recovery-race") as executor:
            recovery = executor.submit(service.execute_progressive_recovery, job_result_id=revision_id)
            renewal = executor.submit(retry)
            assert recovery.result(timeout=10)["status"] == "failed"
            renewal.result(timeout=10)
        with Session(engine) as db:
            state = read_progressive_state(db.get(JobResult, revision_id).document_metadata)
            assert state is not None
            assert state.enhancement.attempt == 2
            assert state.enhancement.status == "pending"
    finally:
        engine.dispose()
