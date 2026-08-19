from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared.core.exceptions.domain_exceptions import UnavailableException


def test_should_skip_duplicate_delivery_when_processing_lock_is_held(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.document_ingestion.processing_run as processing_run

    class FakeLock:
        def __init__(self, _redis_service: object, _job_id: str) -> None:
            pass

        def __enter__(self) -> "FakeLock":
            raise UnavailableException(
                internal_message="Could not acquire processing lock for job job-lock",
                retry_after=120,
            )

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(
        processing_run,
        "load_parse_job_context",
        lambda *args, **kwargs: SimpleNamespace(redis_service=object()),
    )
    monkeypatch.setattr(processing_run, "mark_job_running", lambda *args: True)
    monkeypatch.setattr(
        processing_run,
        "get_sync_job_lifecycle_service",
        object,
    )
    monkeypatch.setattr(processing_run, "RedisJobLock", FakeLock)

    result = processing_run.DocumentProcessingRun().execute(
        job_id="job-lock",
        user_id="contract-user",
    )

    assert result == {
        "status": "skipped",
        "job_id": "job-lock",
        "reason": "job_already_processing",
    }


def test_should_not_treat_other_unavailable_errors_as_lock_contention(
    worker_contract_environment: None,
) -> None:
    import app.services.document_ingestion.processing_run as processing_run

    error = UnavailableException(
        internal_message="Job state is still settling",
        retry_after=120,
    )

    assert processing_run._is_processing_lock_contention(error) is False
