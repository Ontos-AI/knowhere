"""Each parsing owner uploads immutable assets under its own storage identity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest


def _seedOwner() -> None:
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.job import Job
    from shared.models.database.user import User
    with get_sync_db_context() as session:
        session.add(User(id="asset-owner", email="asset-owner@example.invalid", name="Asset owner"))
        session.flush()
        session.add(Job(job_id="owned-assets", user_id="asset-owner", job_type="document_ingestion",
            source_type="file", status="running", parsing_status="running", parsing_owner_token="old-owner",
            parsing_owner_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            job_metadata={"document_id": "doc_owned_assets", "namespace": "default", "source_file_name": "report.pdf"}))


def _uploadOwner(directory: Path, token: str) -> str:
    from app.services.document_ingestion.parse_result_package import GeneratedResultPackage, ParseArtifact, ParseResultPackage
    from app.services.document_ingestion.processing_context import ParseJobContext
    from app.services.document_ingestion.success_finalization import _upload_result_package
    from shared.core.config import settings
    from shared.services.storage.result_storage import JobResultStorage

    source: Path = directory / token
    (source / "images").mkdir(parents=True, exist_ok=True)
    (source / "images/shared.png").write_bytes(token.encode())
    archive: Path = directory / (token + ".zip")
    archive.write_bytes(token.encode())
    package = ParseResultPackage(artifact=ParseArtifact(add_dir=str(source), dataframe=pd.DataFrame()),
        chunks=[{"chunk_id": "asset", "type": "image", "content": "Image", "path": "images/shared.png", "metadata": {"file_path": "images/shared.png"}}])
    context = ParseJobContext(job_metadata={"document_id": "doc_owned_assets", "namespace": "default"},
        job_user_id="asset-owner", metadata_service=SimpleNamespace(update_metadata=lambda *arguments: None),
        redis_service=SimpleNamespace(), s3_key="uploads/report.pdf", parsingOwnerToken=token)
    storage = JobResultStorage(results_bucket=settings.S3_BUCKET_NAME, upload_concurrency=1)
    return _upload_result_package(result_package=package,
        generated_package=GeneratedResultPackage(zip_file_path=str(archive), checksum_value=token, statistics={}, zip_size=10),
        job_id="owned-assets", result_storage_factory=lambda: storage, job_context=context)


def test_late_old_owner_upload_cannot_overwrite_selected_result_assets(
    worker_contract_environment: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from shared.core.config import settings
    from shared.core.config.storage import get_cached_storage_adapter
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer

    _seedOwner()
    oldKey: str = _uploadOwner(tmp_path, "old-owner")
    with get_sync_db_context() as session:
        job = session.get(Job, "owned-assets")
        assert job is not None
        job.parsing_owner_token = "new-owner"
    newKey: str = _uploadOwner(tmp_path, "new-owner")
    assert oldKey != newKey
    assert get_cached_storage_adapter().download_fileobj(oldKey, bucket=settings.S3_BUCKET_NAME) == b"old-owner"
    assert get_cached_storage_adapter().download_fileobj(newKey, bucket=settings.S3_BUCKET_NAME) == b"new-owner"
    monkeypatch.setattr(settings, "KNOWHERE_LEXICAL_BACKEND", "opensearch")
    with get_sync_db_context() as session:
        finalization = SyncJobSuccessFinalizer().finalize(session, job_id="owned-assets",
            result_s3_key=newKey, checksum="new-owner", zip_size=10, chunks=[], stored_count=0,
            delivery_mode="url", section_summaries=None, parsingOwnerToken="new-owner")
        assert finalization.response.should_commit()
        result = session.query(JobResult).filter_by(job_id="owned-assets").one()
        assert result.document_metadata["result_raw_prefix"].endswith("new-owner/")
    with pytest.raises(RuntimeError, match="ownership"):
        _uploadOwner(tmp_path, "old-owner")


@pytest.mark.parametrize("callbackToken", ["old-owner", None])
def test_late_old_parser_failure_cannot_fail_or_refund_new_owner(
    worker_contract_environment: None, callbackToken: str | None,
) -> None:
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.job import Job
    from shared.services.jobs.lifecycle.failure_finalizer import SyncJobFailureFinalizer
    _seedOwner()
    with get_sync_db_context() as session:
        job = session.get(Job, "owned-assets")
        assert job is not None
        job.parsing_owner_token = "new-owner"
        job.billing_status = "charged"
        job.credits_charged = 123456
    with get_sync_db_context() as session:
        outcome = SyncJobFailureFinalizer().finalize(session, job_id="owned-assets", error_message="Late parser error",
            error_code="INTERNAL_ERROR", error_details=None, should_refund=True, parsingOwnerToken=callbackToken)
        assert not outcome.succeeded
    with get_sync_db_context() as session:
        job = session.get(Job, "owned-assets")
        assert job is not None and job.status == "running" and job.parsing_status == "running"
        assert job.billing_status == "charged" and job.credits_charged == 123456


def test_stale_parser_cannot_overwrite_skipped_billing(
    worker_contract_environment: None,
) -> None:
    from app.services.document_ingestion.processing_billing import record_skipped_parse_job_billing
    from app.services.document_ingestion.page_estimator import WorkloadEstimate
    from shared.services.jobs.parsing_error import ParsingOwnerError
    _seedOwner()
    with pytest.raises(ParsingOwnerError):
        record_skipped_parse_job_billing(job_id="owned-assets", workload_estimate=WorkloadEstimate(10, "fixture"),
            parsingOwnerToken="stale-owner")


def test_parsed_finalization_refreshes_the_locked_job_owner(
    worker_contract_environment: None,
) -> None:
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.job import Job
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer
    from shared.services.jobs.parsing_error import ParsingOwnerError

    _seedOwner()
    with get_sync_db_context() as stale_session:
        stale_job: Job | None = stale_session.get(Job, "owned-assets")
        assert stale_job is not None and stale_job.parsing_owner_token == "old-owner"
        with get_sync_db_context() as current_session:
            current_job: Job | None = current_session.get(Job, "owned-assets")
            assert current_job is not None
            current_job.parsing_owner_token = "new-owner"

        with pytest.raises(ParsingOwnerError, match="ownership"):
            SyncJobSuccessFinalizer().finalize(
                stale_session,
                job_id="owned-assets",
                result_s3_key="results/owned-assets.zip",
                checksum="checksum",
                zip_size=10,
                chunks=[],
                stored_count=0,
                delivery_mode="url",
                section_summaries=None,
                parsingOwnerToken="old-owner",
            )
