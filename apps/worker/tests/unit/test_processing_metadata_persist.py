from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import app.services.document_ingestion.processing_context as processing_context  # noqa: E402
from app.services.document_ingestion.success_finalization import (  # noqa: E402
    _record_processing_completion,
)


class _FakeDbContext:
    def __init__(self, session: object) -> None:
        self.session = session

    def __enter__(self) -> object:
        return self.session

    def __exit__(self, *_args: object) -> bool:
        return False


def _job_context(*, metadata: dict[str, object] | None = None) -> processing_context.ParseJobContext:
    return processing_context.ParseJobContext(
        job_metadata=metadata or {"namespace": "default"},
        job_user_id="user-1",
        metadata_service=Mock(),
        redis_service=object(),
        s3_key="uploads/job.pdf",
    )


def test_persist_job_metadata_updates_merges_stages_into_job_row(
    monkeypatch: object,
) -> None:
    job = SimpleNamespace(job_metadata={"namespace": "default", "page_count": 12})
    session = Mock()
    monkeypatch.setattr(
        processing_context,
        "get_sync_db_context",
        lambda: _FakeDbContext(session),
    )
    monkeypatch.setattr(
        processing_context,
        "_select_job_row_for_update",
        lambda _db, _job_id: job,
    )
    job_context = _job_context()
    stages = {
        "token_usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14, "calls": 1},
        "timing_ms": {"worker.parse.document": 1200},
    }

    processing_context.persist_job_metadata_updates(
        job_id="job_abc",
        job_context=job_context,
        metadata_updates={"stages": stages},
    )

    assert job.job_metadata["namespace"] == "default"
    assert job.job_metadata["page_count"] == 12
    assert job.job_metadata["stages"]["token_usage"]["total_tokens"] == 14
    job_context.metadata_service.update_metadata.assert_called_once_with(
        "job_abc",
        {"stages": stages},
    )
    assert job_context.job_metadata["stages"] == stages


def test_record_processing_completion_persists_token_usage_to_job_row(
    monkeypatch: object,
) -> None:
    import app.services.document_ingestion.success_finalization as success_finalization

    persist = Mock()
    monkeypatch.setattr(success_finalization, "persist_job_metadata_updates", persist)
    monkeypatch.setattr(
        success_finalization,
        "get_current_token_tracker",
        lambda: {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10, "calls": 1},
    )
    monkeypatch.setattr(
        success_finalization,
        "get_current_stage_tracker",
        lambda: {"worker.parse.document": 900},
    )
    from datetime import datetime, timezone

    job_context = _job_context()
    started = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)

    _record_processing_completion(
        job_id="job_abc",
        job_context=job_context,
        processing_started_at=started,
    )

    persist.assert_called_once()
    updates = persist.call_args.kwargs["metadata_updates"]
    assert updates["stages"]["token_usage"]["total_tokens"] == 10
    assert "processing_completed_at" in updates
    assert "processing_duration_ms" in updates
