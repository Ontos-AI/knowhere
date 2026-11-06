from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared.core.exceptions.domain_exceptions import UnavailableException


def test_should_skip_duplicate_delivery_when_processing_lock_is_held(
    worker_contract_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.document_ingestion.processing_run as processing_run

    monkeypatch.setattr(processing_run.ParsingOwnership, "getDisposition", lambda self, jobId: "parse")

    class FakeLock:
        def __init__(self, _redis_service: object, _job_id: str, **options: object) -> None:
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


def test_parsed_redelivery_returns_before_loading_source_context(
    worker_contract_environment: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.job import Job
    from shared.models.database.user import User
    import app.services.document_ingestion.processing_run as processing_run

    with get_sync_db_context() as session:
        session.add(User(id="parsed-redelivery-user", email="parsed-redelivery@example.invalid", name="Parsed delivery"))
        session.flush()
        session.add(Job(job_id="parsed-redelivery", user_id="parsed-redelivery-user",
            job_type="document_ingestion", source_type="file", status="running",
            parsing_status="completed", indexing_status="running",
            job_metadata={"document_id": "doc_redelivery", "namespace": "default"}))

    def rejectSourceContext(*arguments: object, **options: object) -> None:
        pytest.fail("A committed parse reentered source preparation")

    monkeypatch.setattr(processing_run, "load_parse_job_context", rejectSourceContext)
    result: dict[str, object] = processing_run.DocumentProcessingRun().execute("parsed-redelivery", "parsed-redelivery-user")
    assert result["status"] == "skipped"
    assert result["reason"] == "parsing_already_completed"


def test_processing_claims_durable_owner_before_running_parse(
    worker_contract_environment: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.job import Job
    from shared.models.database.user import User
    from app.services.document_ingestion.processing_context import ParseJobContext
    import app.services.document_ingestion.processing_run as processing_run

    with get_sync_db_context() as session:
        session.add(User(id="owned-parser-user", email="owned-parser@example.invalid", name="Owned parser"))
        session.flush()
        session.add(Job(job_id="owned-parser", user_id="owned-parser-user", job_type="document_ingestion",
            source_type="file", status="running", job_metadata={"document_id": "doc_owned_parser", "namespace": "default"}))

    class FakeLock:
        def __init__(self, *arguments: object, **options: object) -> None:
            pass
        def __enter__(self) -> FakeLock:
            return self
        def __exit__(self, *arguments: object) -> bool:
            return False

    class FakeWorkspace:
        def cleanup(self) -> bool:
            return True

    def inspectParse(**options: object) -> dict[str, object]:
        context: ParseJobContext = options["job_context"]
        with get_sync_db_context() as session:
            job = session.get(Job, "owned-parser")
            assert job is not None and job.parsing_owner_token is not None
            assert job.parsing_owner_token == getattr(context, "parsingOwnerToken", None)
            assert job.parsing_owner_expires_at is not None
        return {"status": "success", "job_id": "owned-parser"}

    context = ParseJobContext(job_metadata={"namespace": "default"}, job_user_id="owned-parser-user",
        metadata_service=SimpleNamespace(), redis_service=SimpleNamespace(), s3_key="uploads/source.pdf")
    monkeypatch.setattr(processing_run, "load_parse_job_context", lambda *arguments: context)
    monkeypatch.setattr(processing_run, "mark_job_running", lambda *arguments: True)
    monkeypatch.setattr(processing_run, "RedisJobLock", FakeLock)
    monkeypatch.setattr(processing_run.TemporaryParseWorkspace, "create", lambda identity: FakeWorkspace())
    monkeypatch.setattr(processing_run, "_run_parse_job", inspectParse)
    assert processing_run.DocumentProcessingRun().execute("owned-parser", "owned-parser-user")["status"] == "success"


def test_processing_rechecks_committed_parse_after_acquiring_redis_lock(
    worker_contract_environment: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.job import Job
    from shared.models.database.user import User
    from app.services.document_ingestion.processing_context import ParseJobContext
    import app.services.document_ingestion.processing_run as processing_run

    with get_sync_db_context() as session:
        session.add(User(id="parse-race-user", email="parse-race@example.invalid", name="Parse race"))
        session.flush()
        session.add(Job(job_id="parse-race", user_id="parse-race-user", job_type="document_ingestion",
            source_type="file", status="running", job_metadata={"document_id": "doc_parse_race", "namespace": "default"}))

    class RacingLock:
        def __init__(self, *arguments: object, **options: object) -> None:
            pass
        def __enter__(self) -> RacingLock:
            with get_sync_db_context() as session:
                job = session.get(Job, "parse-race")
                assert job is not None
                job.parsing_status = "completed"
            return self
        def __exit__(self, *arguments: object) -> bool:
            return False

    def rejectWorkspace(identity: str) -> None:
        pytest.fail("Parsing committed while waiting for ownership, but a workspace was created")

    context = ParseJobContext(job_metadata={"namespace": "default"}, job_user_id="parse-race-user",
        metadata_service=SimpleNamespace(), redis_service=SimpleNamespace(), s3_key="uploads/source.pdf")
    monkeypatch.setattr(processing_run, "load_parse_job_context", lambda *arguments: context)
    monkeypatch.setattr(processing_run, "mark_job_running", lambda *arguments: True)
    monkeypatch.setattr(processing_run, "RedisJobLock", RacingLock)
    monkeypatch.setattr(processing_run.TemporaryParseWorkspace, "create", rejectWorkspace)
    result = processing_run.DocumentProcessingRun().execute("parse-race", "parse-race-user")
    assert result["status"] == "skipped"
    assert result["reason"] == "parsing_already_completed"
