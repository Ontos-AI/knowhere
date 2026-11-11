from datetime import timedelta

from celery import Celery
from pytest import MonkeyPatch
from sqlalchemy import update


def test_should_recover_an_unacknowledged_upload_without_restarting_other_jobs(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.core.tasks import upload_parse_dispatch_tasks
    from shared.core.celery_router import task_router
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.job import Job
    from shared.models.database.upload_parse_dispatch import UploadParseDispatch
    from shared.models.database.user import User
    from shared.services.jobs import upload_parse_publisher
    from shared.services.jobs.upload_parse_dispatch import UploadParseOutbox
    from shared.utils.utc_now import utc_now_naive

    user_id = "upload-dispatch-owner"
    job_ids = ("interrupted-send", "already-running", "expired", "retried-job")
    with get_sync_db_context() as db:
        db.add(
            User(
                id=user_id,
                name="Dispatch owner",
                email="upload-dispatch@example.invalid",
            )
        )
        db.flush()
        db.add_all(
            Job(
                job_id=job_id,
                user_id=user_id,
                job_type="document_ingestion",
                source_type="file",
                status="pending",
            )
            for job_id in job_ids
        )
        db.flush()
        initial_claims = {
            job_id: UploadParseOutbox.stage(db, job_id=job_id, user_id=user_id)
            for job_id in job_ids
        }

    with get_sync_db_context() as db:
        assert UploadParseOutbox.claim_due(db) is None
        db.execute(update(UploadParseDispatch).values(available_at=utc_now_naive()))
        db.execute(
            update(Job).where(Job.job_id == "already-running").values(status="running")
        )
        db.execute(
            update(Job)
            .where(Job.job_id == "expired")
            .values(updated_at=utc_now_naive() - timedelta(days=1))
        )
        db.execute(update(Job).where(Job.job_id == "retried-job").values(version=2))

    publications: list[tuple[str, str]] = []
    broker = Celery("upload-dispatch-test", broker="memory://", set_as_current=False)
    monkeypatch.setattr(upload_parse_publisher, "get_celery_app", lambda: broker)

    def _publish(*, job_id: str, user_id: str) -> str:
        publications.append((job_id, user_id))
        with get_sync_db_context() as other_db:
            assert UploadParseOutbox.claim_due(other_db) is None
            # A delayed acknowledgement for the initial attempt cannot settle
            # the newer attempt's claim.
            UploadParseOutbox.finish(
                other_db, initial_claims["interrupted-send"], published=True
            )
        if len(publications) == 1:
            raise ConnectionError("broker unavailable during recovery")
        return upload_parse_publisher.publish_upload_parse(
            job_id=job_id, user_id=user_id
        )

    monkeypatch.setattr(upload_parse_dispatch_tasks, "publish_upload_parse", _publish)
    assert upload_parse_dispatch_tasks.recover_upload_parse_dispatches() == 0
    with get_sync_db_context() as db:
        dispatch = db.get(UploadParseDispatch, "interrupted-send")
        assert dispatch is not None
        assert dispatch.attempt == 2
        assert dispatch.published_at is None
        assert dispatch.available_at > utc_now_naive()
        dispatch.available_at = utc_now_naive()

    assert upload_parse_dispatch_tasks.recover_upload_parse_dispatches() == 1
    assert upload_parse_dispatch_tasks.recover_upload_parse_dispatches() == 0
    assert publications == [
        ("interrupted-send", user_id),
        ("interrupted-send", user_id),
    ]
    queue_name = task_router.get_queue_for_job("document_ingestion", user_id)
    with broker.connection_for_read() as connection:
        with connection.SimpleQueue(queue_name) as queue:
            message = queue.get(block=False)
            assert (
                message.headers["task"]
                == "app.core.tasks.document_ingestion_tasks.parse_task"
            )
            assert message.payload[:2] == [
                ["interrupted-send"],
                {"user_id": user_id, "job_type": "document_ingestion"},
            ]
            message.ack()
            assert queue.qsize() == 0
    with get_sync_db_context() as db:
        dispatch = db.get(UploadParseDispatch, "interrupted-send")
        assert dispatch is not None
        assert dispatch.attempt == 3
        assert dispatch.published_at is not None
