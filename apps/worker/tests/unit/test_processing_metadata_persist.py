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
import app.services.document_ingestion.success_finalization as success_finalization  # noqa: E402


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

    success_finalization._record_processing_completion(
        job_id="job_abc",
        job_context=job_context,
        processing_started_at=started,
    )

    persist.assert_called_once()
    updates = persist.call_args.kwargs["metadata_updates"]
    assert updates["stages"]["token_usage"]["total_tokens"] == 10
    assert "processing_completed_at" in updates
    assert "processing_duration_ms" in updates


def test_run_parse_job_failure_persists_token_usage(monkeypatch: object) -> None:
    import app.services.document_ingestion.processing_run as processing_run

    persisted: dict[str, object] = {}

    def fake_persist(*, job_id: str, job_context: object, metadata_updates: dict[str, object]) -> None:
        persisted.update(metadata_updates)

    monkeypatch.setattr(processing_run, "persist_job_metadata_updates", fake_persist)
    monkeypatch.setattr(
        processing_run,
        "init_token_tracker",
        lambda: {"prompt_tokens": 6, "completion_tokens": 1, "total_tokens": 7, "calls": 1},
    )
    monkeypatch.setattr(processing_run, "init_stage_tracker", lambda: {"worker.parse.document": 12})
    monkeypatch.setattr(processing_run, "init_llm_overrides", lambda *_args: None)
    monkeypatch.setattr(processing_run, "cleanup_llm_overrides", lambda: None)
    monkeypatch.setattr(processing_run, "cleanup_token_tracker", lambda: None)
    monkeypatch.setattr(processing_run, "cleanup_stage_tracker", lambda: None)

    def _boom(**_kwargs: object) -> object:
        raise RuntimeError("parse failed after LLM work")

    monkeypatch.setattr(processing_run, "prepare_source_file", _boom)

    lifecycle = Mock()
    job_context = _job_context()
    try:
        processing_run._run_parse_job(
            job_id="job-fail",
            job_context=job_context,
            lifecycle_service=lifecycle,
            task_workspace=SimpleNamespace(
                input_dir="/tmp",
                output_dir="/tmp",
                root_dir="/tmp",
            ),
        )
    except RuntimeError as exc:
        assert "parse failed after LLM work" in str(exc)
    else:
        raise AssertionError("expected parse failure")

    stages = persisted["stages"]
    assert isinstance(stages, dict)
    assert stages["token_usage"]["total_tokens"] == 7
    assert stages["timing_ms"]["worker.parse.document"] == 12
