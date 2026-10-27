from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("S3_BUCKET_NAME", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")


def test_late_failure_does_not_change_a_newer_running_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.page_memory import scope_enrichment_service as service
    from shared.models.database.document import DocumentScopeEnrichment

    engine = create_engine("sqlite://")
    DocumentScopeEnrichment.__table__.create(engine)
    with Session(engine) as db:
        db.add(
            DocumentScopeEnrichment(
                id="scope",
                document_id="doc",
                job_result_id="result",
                scope_id="p1-2",
                page_start=1,
                page_end=2,
                state="running",
                attempts=2,
                claim_generation="new",
            )
        )
        db.commit()

    @contextmanager
    def database():
        with Session(engine) as db:
            yield db
            db.commit()

    monkeypatch.setattr(service, "get_sync_db_context", database)
    service._mark_scope_failed(
        job_result_id="result", scope_id="p1-2", claim_generation="old"
    )
    with Session(engine) as db:
        assert db.scalars(select(DocumentScopeEnrichment)).one().state == "running"


def test_immutable_snapshot_upload_uses_separate_archive_and_asset_keys(
    tmp_path: Path,
) -> None:
    from shared.services.storage.result_storage import JobResultStorage

    class Adapter:
        def __init__(self) -> None:
            self.uploaded: list[str] = []

        def upload_file(
            self, local_path: str, key: str, bucket: str | None = None
        ) -> dict[str, str]:
            self.uploaded.append(key)
            return {"key": key}

    adapter = Adapter()
    storage = JobResultStorage(results_bucket="test", storage_adapter=adapter)
    directory = tmp_path / "result"
    (directory / "images").mkdir(parents=True)
    (directory / "images/page.jpg").write_bytes(b"image")
    archive = tmp_path / "result.zip"
    archive.write_bytes(b"baseline")
    baseline = storage.upload(
        job_id="job",
        snapshot_id="baseline",
        result_dir=str(directory),
        zip_file_path=str(archive),
        artifact_refs={"images/page.jpg"},
    )
    archive.write_bytes(b"final")
    final = storage.upload(
        job_id="job",
        snapshot_id="final",
        result_dir=str(directory),
        zip_file_path=str(archive),
        artifact_refs={"images/page.jpg"},
    )
    assert baseline.zip_key != final.zip_key
    assert baseline.raw_prefix != final.raw_prefix
    assert baseline.raw_files["images/page.jpg"] != final.raw_files["images/page.jpg"]


def test_repair_page_seven_preserves_other_vectors_and_rebinds_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import Column, Integer, JSON, MetaData, String, Table
    from sqlalchemy.orm import registry
    from types import SimpleNamespace
    from shared.services.retrieval import page_embedding_index as index
    from shared.services.retrieval.publication_models import DocumentPublicationScope

    mapper = registry(metadata=MetaData())

    class Chunk:
        pass

    class Embedding:
        pass

    chunk_table = Table(
        "chunks",
        mapper.metadata,
        Column("id", String, primary_key=True),
        Column("chunk_id", String),
        Column("document_id", String),
        Column("job_result_id", String),
        Column("chunk_type", String),
        Column("chunk_metadata", JSON),
        Column("sort_order", Integer),
    )
    embedding_table = Table(
        "vectors",
        mapper.metadata,
        Column("id", String, primary_key=True),
        Column("user_id", String),
        Column("namespace", String),
        Column("document_id", String),
        Column("job_result_id", String),
        Column("chunk_id", String),
        Column("page_num", Integer),
        Column("modality", String),
        Column("model", String),
        Column("dims", Integer),
        Column("embedding", JSON),
    )
    mapper.map_imperatively(Chunk, chunk_table)
    mapper.map_imperatively(Embedding, embedding_table)
    monkeypatch.setattr(
        index.CorpusStorage,
        "resolve_namespace",
        lambda _: SimpleNamespace(DocumentChunk=Chunk, DocumentPageEmbedding=Embedding),
    )
    engine = create_engine("sqlite://")
    mapper.metadata.create_all(engine)
    scope = DocumentPublicationScope(
        user_id="user",
        namespace="default",
        document_id="doc",
        job_result_id="result",
        source_file_name="scan.pdf",
    )
    with Session(engine) as db:
        for page in range(1, 9):
            db.add(
                Chunk(
                    id=str(page),
                    chunk_id=f"new-{page}",
                    document_id="doc",
                    job_result_id="result",
                    chunk_type="page",
                    chunk_metadata={"page_nums": [page]},
                    sort_order=page,
                )
            )
            if page != 7:
                db.add(
                    Embedding(
                        id=str(page),
                        document_id="doc",
                        job_result_id="result",
                        page_num=page,
                        chunk_id=f"old-{page}",
                        embedding=[float(page)],
                    )
                )
        db.commit()
        index.upsert_document_page_embeddings(
            db, scope=scope, incoming_embeddings_by_page={7: [7.0]}
        )
        db.commit()
        rows = db.scalars(select(Embedding).order_by(Embedding.page_num)).all()
        assert [row.page_num for row in rows] == list(range(1, 9))
        assert [row.chunk_id for row in rows] == [f"new-{page}" for page in range(1, 9)]
        assert [row.embedding for row in rows] == [
            [float(page)] for page in range(1, 9)
        ]


def test_publication_cache_version_changes_only_after_sql_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.services.retrieval import publication_scope_content as publication

    calls: list[str] = []
    monkeypatch.setattr(
        publication,
        "bump_retrieval_namespace_cache_version_sync",
        lambda **kwargs: calls.append(str(kwargs["namespace"])),
    )
    engine = create_engine("sqlite://")
    with Session(engine) as db:
        publication._invalidate_namespace_after_commit(
            db, user_id="user", namespace="default"
        )
        assert calls == []
        db.commit()
        assert calls == ["default"]
    with Session(engine) as db:
        publication._invalidate_namespace_after_commit(
            db, user_id="user", namespace="rolled-back"
        )
        db.rollback()
        db.commit()
        assert calls == ["default"]


def test_stage_claims_are_bounded_and_late_completion_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.page_memory import scope_enrichment_service as service
    from shared.models.database.job_result import JobResult

    engine = create_engine("sqlite://")
    from shared.models.database.job import Job

    Job.__table__.create(engine)
    JobResult.__table__.create(engine)
    from shared.models.database.job_result import JobChunk

    JobChunk.__table__.create(engine)
    with Session(engine) as db:
        db.add(
            JobResult(
                id="result",
                job_id="job",
                delivery_mode="url",
                document_metadata={
                    "progressive": {
                        "enhancement": {"status": "pending", "attempt": 1},
                        "snapshots": [],
                        "stages": {"finalization": {"status": "failed", "attempts": 2}},
                    }
                },
            )
        )
        db.commit()

    @contextmanager
    def database():
        with Session(engine) as db:
            yield db
            db.commit()

    monkeypatch.setattr(service, "get_sync_db_context", database)
    token = service._claim_progressive_stage(
        job_result_id="result", stage_name="finalization"
    )
    assert token
    assert (
        service._finish_progressive_stage(
            job_result_id="result",
            stage_name="finalization",
            claim_generation="obsolete",
            error=None,
        )
        is False
    )
    assert (
        service._finish_progressive_stage(
            job_result_id="result",
            stage_name="finalization",
            claim_generation=token,
            error="upload failed",
        )
        is True
    )
    assert (
        service._claim_progressive_stage(
            job_result_id="result", stage_name="finalization"
        )
        is None
    )
    with Session(engine) as db:
        state = db.get(JobResult, "result").document_metadata["progressive"]
        assert state["enhancement"]["status"] == "failed"
        assert state["enhancement"]["can_retry"] is True


def test_snapshot_upload_rejects_missing_required_assets_before_publication(
    tmp_path: Path,
) -> None:
    from shared.services.storage.result_storage import JobResultStorage

    class Adapter:
        def __init__(self) -> None:
            self.uploaded: list[str] = []

        def upload_file(
            self, local_path: str, key: str, bucket: str | None = None
        ) -> dict[str, str]:
            self.uploaded.append(key)
            return {"key": key}

    adapter = Adapter()
    storage = JobResultStorage(results_bucket="test", storage_adapter=adapter)
    directory = tmp_path / "result"
    directory.mkdir()
    archive = tmp_path / "baseline.zip"
    archive.write_bytes(b"zip")
    with pytest.raises(ValueError, match="required.*images/missing.jpg"):
        storage.upload(
            job_id="job",
            snapshot_id="baseline",
            result_dir=str(directory),
            zip_file_path=str(archive),
            artifact_refs={"images/missing.jpg"},
        )
    assert adapter.uploaded == []


def test_scope_attempt_assets_are_isolated_and_all_published_references_follow_them(
    tmp_path: Path,
) -> None:
    import pandas as pd
    from app.services.page_memory import scope_enrichment_service as service

    root = tmp_path / "document"
    files = {
        "images/figure.png": b"image",
        "tables/table.html": b"<table><img src='../images/figure.png'></table>",
        "page_citation_assets/page.jpg": b"page",
    }
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    content = "Before [images/figure.png] after [tables/table.html]"
    output = service.ScopeRefinementOutput(
        scope_id="p1-2",
        dataframe=pd.DataFrame(),
        chunks=[
            {
                "chunk_id": "page",
                "type": "page",
                "content": content,
                "metadata": {
                    "page_assets": [{"artifact_ref": "page_citation_assets/page.jpg"}],
                    "connect_to": [
                        {
                            "ref": "[images/figure.png]",
                            "position": {"start": 7, "end": 26},
                        }
                    ],
                },
            },
            {
                "chunk_id": "image",
                "type": "image",
                "path": "images/figure.png",
                "content": "[images/figure.png]",
                "metadata": {"file_path": "images/figure.png"},
            },
            {
                "chunk_id": "table",
                "type": "table",
                "path": "tables/table.html",
                "metadata": {"file_path": "tables/table.html"},
            },
        ],
        section_summaries={},
        artifact_refs=set(files),
    )
    frozen = service._freeze_scope_artifacts(
        output=output, workspace_dir=str(tmp_path), claim_generation="claim-new"
    )
    assert len(frozen.artifact_refs) == 3
    assert all(
        reference.split("/")[1].startswith("claim-new-")
        for reference in frozen.artifact_refs
    )
    assert all(len(reference.split("/")) == 2 for reference in frozen.artifact_refs)
    assert (
        frozen.chunks[0]["metadata"]["page_assets"][0]["artifact_ref"]
        in frozen.artifact_refs
    )
    assert frozen.chunks[1]["path"] in frozen.artifact_refs
    assert frozen.chunks[1]["metadata"]["file_path"] == frozen.chunks[1]["path"]
    connection = frozen.chunks[0]["metadata"]["connect_to"][0]
    assert (
        frozen.chunks[0]["content"][
            connection["position"]["start"] : connection["position"]["end"]
        ]
        == connection["ref"]
    )
    for reference in frozen.artifact_refs:
        assert (root / reference).is_file()
    assert output.chunks[1]["path"] == "images/figure.png"
    table_reference = frozen.chunks[2]["metadata"]["file_path"]
    assert frozen.chunks[1]["path"].encode() in (root / table_reference).read_bytes()
    from shared.services.storage.zip_result_service import ZipResultService
    from zipfile import ZipFile

    (root / "source.pdf").write_bytes(b"%PDF-source")
    archive_path, _, _, _ = ZipResultService().generate_zip_package(
        job_id="job",
        chunks=frozen.chunks,
        add_dir=str(root),
        source_file_name="scan.pdf",
        data_id=None,
        job_metadata={"ingestion_mode": "progressive"},
        temp_dir=str(tmp_path),
    )
    with ZipFile(archive_path) as archive:
        assert frozen.artifact_refs <= set(archive.namelist())
        for reference in frozen.artifact_refs:
            assert archive.read(reference) == (root / reference).read_bytes()


def test_phase_b_download_uses_committed_attempt_mapping_and_keeps_local_canonical_names(
    tmp_path: Path,
) -> None:
    from app.services.page_memory import scope_enrichment_service as service

    class Storage:
        def download_raw_to_temp(
            self, *, relative_path: str, temp_dir: str, **kwargs: object
        ) -> str:
            path = Path(temp_dir) / "downloaded.json"
            path.write_text(
                "new manifest"
                if relative_path == "phase_b/claim-new/manifest.json"
                else "stale manifest"
            )
            return str(path)

    service._download_phase_b_inputs(
        storage=Storage(),
        job_id="job",
        raw_prefix="results/job/",
        job_metadata={
            "page_memory_phase_b": ["phase_b/manifest.json"],
            "page_memory_phase_b_artifacts": {
                "phase_b/manifest.json": "phase_b/claim-new/manifest.json"
            },
        },
        output_dir=str(tmp_path),
    )
    assert (tmp_path / "phase_b/manifest.json").read_text() == "new manifest"


def test_late_structure_upload_cannot_replace_committed_phase_b_input_bytes(
    tmp_path: Path,
) -> None:
    from app.services.page_memory import scope_enrichment_service as service

    root = tmp_path / "document"
    source = root / "phase_b/manifest.json"
    source.parent.mkdir(parents=True)
    files: dict[str, bytes] = {}

    class Storage:
        def upload_raw_file(
            self, *, relative_path: str, local_file_path: str, **kwargs: object
        ) -> None:
            files[relative_path] = Path(local_file_path).read_bytes()

        def download_raw_to_temp(
            self, *, relative_path: str, temp_dir: str, **kwargs: object
        ) -> str:
            downloaded = Path(temp_dir) / "downloaded.json"
            downloaded.write_bytes(files[relative_path])
            return str(downloaded)

    storage = Storage()
    selected = {"phase_b/manifest.json": "phase_b/new/manifest.json"}
    source.write_bytes(b"committed manifest")
    service._upload_scope_artifacts(
        storage=storage,
        job_id="job",
        raw_prefix="results/job/",
        workspace_dir=str(tmp_path),
        artifact_refs=set(selected),
        artifact_mapping=selected,
    )
    source.write_bytes(b"late stale manifest")
    service._upload_scope_artifacts(
        storage=storage,
        job_id="job",
        raw_prefix="results/job/",
        workspace_dir=str(tmp_path),
        artifact_refs=set(selected),
        artifact_mapping={"phase_b/manifest.json": "phase_b/old/manifest.json"},
    )
    service._download_phase_b_inputs(
        storage=storage,
        job_id="job",
        raw_prefix="results/job/",
        job_metadata={
            "page_memory_phase_b": list(selected),
            "page_memory_phase_b_artifacts": selected,
        },
        output_dir=str(root),
    )
    assert source.read_bytes() == b"committed manifest"
    assert files["phase_b/old/manifest.json"] == b"late stale manifest"
