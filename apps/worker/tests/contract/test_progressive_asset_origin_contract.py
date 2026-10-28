"""Progressive publication serves immutable assets and transfers only resume inputs."""

from __future__ import annotations

import hashlib
import asyncio
import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("S3_BUCKET_NAME", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")


class MemoryObjects:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploaded: list[str] = []

    def upload_file(
        self, local_path: str, key: str, bucket: str | None = None
    ) -> dict[str, str]:
        self.objects[key] = Path(local_path).read_bytes()
        self.uploaded.append(key)
        return {"key": key}

    def download_file(
        self, key: str, local_path: str, bucket: str | None = None
    ) -> str:
        Path(local_path).write_bytes(self.objects[key])
        return local_path

    def generate_presigned_url(self, key: str, **kwargs: object) -> str:
        assert key in self.objects
        return f"https://storage.invalid/{key}"


def test_baseline_images_are_put_once_and_result_writer_selects_serving_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.document_ingestion import success_finalization as finalization
    from app.services.document_ingestion.parse_result_package import (
        GeneratedResultPackage,
        ParseArtifact,
        ParseResultPackage,
    )
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.services.jobs.lifecycle.result_writer import SyncJobResultWriter
    from shared.services.storage.result_storage import JobResultStorage

    root: Path = tmp_path / "document"
    files: dict[str, bytes] = {
        "page_citation_assets/page-1.jpg": b"baseline image",
        "source.pdf": b"%PDF-original",
        "phase_b/blackboard.json": b"{}",
        "phase_b/page_full_text_cache.json": b'{"1":"text"}',
    }
    for relative, content in files.items():
        path: Path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    archive: Path = tmp_path / "baseline.zip"
    with ZipFile(archive, "w") as package:
        for name in ("manifest.json", "chunks.json", "doc_nav.json"):
            package.writestr(name, "{}")
        for relative, content in files.items():
            if not relative.startswith("phase_b/"):
                package.writestr(relative, content)
    metadata: dict[str, object] = {
        "parse_track": "page_memory",
        "ingestion_mode": "progressive",
    }

    def persist(**kwargs: object) -> None:
        metadata.update(kwargs["metadata_updates"])

    monkeypatch.setattr(finalization, "persist_job_metadata_updates", persist)
    objects: MemoryObjects = MemoryObjects()
    storage: JobResultStorage = JobResultStorage(
        results_bucket="test", storage_adapter=objects
    )
    chunks: list[dict[str, object]] = [
        {
            "chunk_id": "page",
            "type": "page",
            "content": "text",
            "metadata": {
                "page_nums": [1],
                "page_assets": [{"artifact_ref": "page_citation_assets/page-1.jpg"}],
            },
        }
    ]
    finalization._upload_result_package(
        result_package=ParseResultPackage(
            ParseArtifact(str(root), pd.DataFrame()), chunks
        ),
        generated_package=GeneratedResultPackage(
            str(archive), "a" * 64, {}, archive.stat().st_size
        ),
        job_id="job",
        result_storage_factory=lambda: storage,
        job_context=SimpleNamespace(job_metadata=metadata),
    )
    image_puts: list[str] = [
        key for key in objects.uploaded if key.endswith("page-1.jpg")
    ]
    assert len(image_puts) == 1, image_puts
    serving_prefix: str = str(metadata["result_serving_raw_prefix"])
    assert "/snapshots/" in serving_prefix
    assert metadata["result_raw_prefix"] == "results/job/"
    assert metadata["page_memory_working_raw_prefix"] == "results/job/"
    assert {
        key
        for key in objects.uploaded
        if key.startswith("results/job/") and "/snapshots/" not in key
    } == {
        f"results/job/{relative}"
        for relative in files
        if not relative.startswith("page_citation_assets/")
    }
    assert (
        objects.objects[f"{serving_prefix}page_citation_assets/page-1.jpg"]
        == files["page_citation_assets/page-1.jpg"]
    )
    engine = create_engine("sqlite://")
    Job.__table__.create(engine)
    JobResult.__table__.create(engine)
    with Session(engine) as database:
        database.add(
            Job(
                job_id="job",
                user_id="owner",
                job_type="document_ingestion",
                source_type="direct_upload",
                job_metadata=metadata,
            )
        )
        database.flush()
        result = SyncJobResultWriter().upsert_job_result(database, "job", "url")
        assert result.document_metadata["result_raw_prefix"] == serving_prefix
    from shared.services.retrieval.hydration import assets, evidence_compose
    from shared.services.retrieval.agent_tools.tools.read import _image_url_media

    monkeypatch.setattr(assets, "get_result_storage", lambda: storage)
    monkeypatch.setattr(evidence_compose, "get_result_storage", lambda: storage)
    row: dict[str, object] = {
        "job_id": "job",
        "chunk_type": "page",
        "result_raw_prefix": serving_prefix,
        "chunk_metadata": {
            "enrichment_state": "text",
            "page_nums": [1],
            "page_assets": [
                {"page_num": 1, "artifact_ref": "page_citation_assets/page-1.jpg"}
            ],
        },
    }
    hydrated_assets = asyncio.run(
        assets._enrich_page_assets(row=row, log_context="contract")
    )
    assets._attach_enriched_page_assets(row=row, page_assets=hydrated_assets)
    assert _image_url_media(row)[0]["url"].endswith(
        f"{serving_prefix}page_citation_assets/page-1.jpg"
    )
    image = evidence_compose._try_read_image_artifact(
        row, "page_citation_assets/page-1.jpg", media_type="image/jpeg"
    )
    assert image is not None
    assert base64.b64decode(image["data"]) == b"baseline image"


def test_snapshot_bootstrap_objects_match_archive_bytes_without_internal_inputs(
    tmp_path: Path,
) -> None:
    from shared.services.storage.result_storage import JobResultStorage

    root: Path = tmp_path / "document"
    (root / "images").mkdir(parents=True)
    (root / "images/page.jpg").write_bytes(b"image")
    (root / "source.pdf").write_bytes(b"%PDF-source")
    (root / "phase_b").mkdir()
    (root / "phase_b/blackboard.json").write_text("private")
    archive: Path = tmp_path / "snapshot.zip"
    archive_objects: dict[str, bytes] = {
        "manifest.json": b'{"version":"2.0"}',
        "chunks.json": b'{"chunks":[]}',
        "doc_nav.json": b'{"sections":[]}',
        "source.pdf": b"%PDF-source",
        "images/page.jpg": b"image",
    }
    with ZipFile(archive, "w") as package:
        for relative, content in archive_objects.items():
            package.writestr(relative, content)
    objects: MemoryObjects = MemoryObjects()
    storage: JobResultStorage = JobResultStorage(
        results_bucket="test", storage_adapter=objects
    )
    bundle = storage.upload(
        job_id="job",
        snapshot_id="baseline",
        result_dir=str(root),
        zip_file_path=str(archive),
        artifact_refs={"images/page.jpg", "phase_b/blackboard.json"},
    )
    for relative, content in archive_objects.items():
        assert objects.objects.get(f"{bundle.raw_prefix}{relative}") == content
    inventory = json.loads(objects.objects[f"{bundle.raw_prefix}snapshot-objects.json"])
    assert {entry["path"] for entry in inventory["objects"]} == set(archive_objects)
    for entry in inventory["objects"]:
        content: bytes = archive_objects[entry["path"]]
        assert entry["size_bytes"] == len(content)
        assert entry["checksum_sha256"] == hashlib.sha256(content).hexdigest()
        assert entry["kind"] == (
            "image" if entry["path"].startswith("images/") else "essential"
        )
    with ZipFile(archive, "w") as package:
        for relative, content in archive_objects.items():
            package.writestr(relative, content)
    repeated = storage.upload(
        job_id="job",
        snapshot_id="final",
        result_dir=str(root),
        zip_file_path=str(archive),
    )
    repeated_inventory = json.loads(
        objects.objects[f"{repeated.raw_prefix}snapshot-objects.json"]
    )
    assert {entry["path"] for entry in repeated_inventory["objects"]} == set(
        archive_objects
    )
    assert objects.uploaded.count(f"{repeated.raw_prefix}snapshot-objects.json") == 1


@pytest.mark.parametrize("has_immutable_origin", [True, False])
def test_another_worker_packages_inherited_baseline_assets_with_staged_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_immutable_origin: bool
) -> None:
    from app.services.page_memory import scope_enrichment_service as service
    from shared.services.storage.result_storage import JobResultStorage

    objects: MemoryObjects = MemoryObjects()
    baseline_prefix: str = (
        "results/job/snapshots/baseline/" if has_immutable_origin else "results/job/"
    )
    objects.objects = {
        f"{baseline_prefix}page_citation_assets/page-1.jpg": b"baseline image",
        f"{baseline_prefix}source.pdf": b"%PDF-original",
        "results/job/images/claim-new-figure.jpg": b"fine image",
    }
    target = SimpleNamespace(
        source_file_name="document.pdf",
        job_id="job",
        document_id="document",
        raw_prefix="results/job/",
        serving_raw_prefix=baseline_prefix,
        job_metadata={
            "ingestion_mode": "progressive",
            "parse_track": "page_memory",
            **(
                {
                    "page_memory_baseline_artifact_refs": [
                        "page_citation_assets/page-1.jpg",
                        "source.pdf",
                    ]
                }
                if has_immutable_origin
                else {}
            ),
        },
    )
    monkeypatch.setattr(service, "enrich_doc_nav_summaries", lambda *args, **kwargs: {})
    snapshot, _size = service._regenerate_and_upload_full_package(
        job_result_id="result",
        target=target,
        storage=JobResultStorage(results_bucket="test", storage_adapter=objects),
        workspace_dir=str(tmp_path),
        snapshot_id="final",
        staged_chunks=[
            {
                "chunk_id": "page",
                "type": "page",
                "content": "text",
                "path": "document.pdf/Root",
                "metadata": {
                    "page_nums": [1],
                    "page_assets": [
                        {"artifact_ref": "page_citation_assets/page-1.jpg"}
                    ],
                },
            },
            {
                "chunk_id": "image",
                "type": "image",
                "content": "figure",
                "path": "images/claim-new-figure.jpg",
                "metadata": {"file_path": "images/claim-new-figure.jpg"},
            },
        ],
    )
    assert snapshot is not None
    final_prefix: str = "results/job/snapshots/final/"
    assert (
        objects.objects[f"{final_prefix}page_citation_assets/page-1.jpg"]
        == b"baseline image"
    )
    assert (
        objects.objects[f"{final_prefix}images/claim-new-figure.jpg"] == b"fine image"
    )
    assert (
        objects.objects[f"{baseline_prefix}page_citation_assets/page-1.jpg"]
        == b"baseline image"
    )
    if has_immutable_origin:
        assert "results/job/page_citation_assets/page-1.jpg" not in objects.objects
        del objects.objects[f"{baseline_prefix}page_citation_assets/page-1.jpg"]
        objects.objects["results/job/page_citation_assets/page-1.jpg"] = (
            b"wrong working revision"
        )
        from shared.core.exceptions.domain_exceptions import StorageServiceException

        with pytest.raises(StorageServiceException):
            service._regenerate_and_upload_full_package(
                job_result_id="result",
                target=target,
                storage=JobResultStorage(
                    results_bucket="test", storage_adapter=objects
                ),
                workspace_dir=str(tmp_path / "another-worker"),
                snapshot_id="final-retry",
                staged_chunks=[
                    {
                        "chunk_id": "page",
                        "type": "page",
                        "content": "text",
                        "path": "document.pdf/Root",
                        "metadata": {
                            "page_nums": [1],
                            "page_assets": [
                                {"artifact_ref": "page_citation_assets/page-1.jpg"}
                            ],
                        },
                    }
                ],
            )
        assert not any("final-retry" in key for key in objects.uploaded)


def test_final_commit_moves_serving_origin_and_retains_baseline_identity() -> None:
    from shared.models.database.job_result import JobResult
    from shared.models.schemas.progressive_ingestion import StoredSnapshot
    from shared.services.jobs.progressive_completion import commit_final_completion

    baseline = StoredSnapshot(
        snapshot_id="baseline",
        stage="baseline",
        job_id="job",
        job_result_id="result",
        document_id="document",
        checksum="a" * 64,
        archive_key="results/job/snapshots/baseline.zip",
    )
    final = StoredSnapshot(
        snapshot_id="final",
        stage="final",
        job_id="job",
        job_result_id="result",
        document_id="document",
        checksum="b" * 64,
        archive_key="results/job/snapshots/final.zip",
    )
    result = JobResult(
        id="result",
        job_id="job",
        document_id="document",
        delivery_mode="url",
        document_metadata={
            "result_raw_prefix": "results/job/snapshots/baseline/",
            "progressive": {
                "snapshots": [baseline.model_dump()],
                "current_snapshot_id": "baseline",
            },
        },
    )
    engine = create_engine("sqlite://")
    with Session(engine) as database:
        commit_final_completion(
            database,
            job_id="job",
            job_result=result,
            snapshot=final,
            outbox=SimpleNamespace(create_event=lambda *args, **kwargs: None),
        )
    assert (
        result.document_metadata["result_raw_prefix"] == "results/job/snapshots/final/"
    )
    assert (
        result.document_metadata["progressive"]["snapshots"][0]["archive_key"]
        == baseline.archive_key
    )
