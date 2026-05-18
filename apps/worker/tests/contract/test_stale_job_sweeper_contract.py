from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

from support.contract_database import insert_contract_job, insert_contract_user


def _build_file_job_metadata() -> dict[str, str]:
    return {
        "document_id": f"doc_{uuid4().hex[:12]}",
        "namespace": "worker-contract",
        "source_type": "file",
    }


def _load_worker_modules() -> tuple[Any, Engine]:
    import app.core.tasks.stale_job_sweeper as stale_job_sweeper
    from shared.core.database_sync import get_sync_engine

    return stale_job_sweeper, get_sync_engine()


def test_should_expire_stale_jobs_and_persist_failure_state(
    worker_contract_environment: None,
) -> None:
    stale_job_id = f"job_stale_{uuid4().hex[:12]}"
    fresh_job_id = f"job_fresh_{uuid4().hex[:12]}"
    user_id = f"worker-user-{uuid4().hex[:12]}"

    stale_job_sweeper, engine = _load_worker_modules()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_updated_at = now - timedelta(days=1)

    with engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        insert_contract_job(
            connection,
            job_id=stale_job_id,
            user_id=user_id,
            status="running",
            source_type="file",
            webhook_enabled=False,
            job_metadata=_build_file_job_metadata(),
            created_at=stale_updated_at - timedelta(minutes=5),
            updated_at=stale_updated_at,
        )
        insert_contract_job(
            connection,
            job_id=fresh_job_id,
            user_id=user_id,
            status="waiting-file",
            source_type="file",
            webhook_enabled=False,
            job_metadata=_build_file_job_metadata(),
            created_at=now - timedelta(minutes=5),
            updated_at=now,
        )

    result = stale_job_sweeper.expire_stale_jobs()

    assert result == {"status": "success", "expired": 1, "skipped": 0}

    with engine.begin() as connection:
        stale_job_row = (
            connection.execute(
                text(
                    """
                    SELECT status, error_code, error_message
                    FROM jobs
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": stale_job_id},
            )
            .mappings()
            .one()
        )
        fresh_job_row = (
            connection.execute(
                text(
                    """
                    SELECT status
                    FROM jobs
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": fresh_job_id},
            )
            .mappings()
            .one()
        )
        audit_log_row = (
            connection.execute(
                text(
                    """
                    SELECT from_state, to_state, transition_reason, transition_metadata
                    FROM job_state_audit_logs
                    WHERE job_id = :job_id
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"job_id": stale_job_id},
            )
            .mappings()
            .one()
        )

    audit_metadata = dict(audit_log_row["transition_metadata"])

    assert stale_job_row["status"] == "failed"
    assert stale_job_row["error_code"] == stale_job_sweeper.JOB_EXPIRED_ERROR_CODE
    assert stale_job_row["error_message"] == stale_job_sweeper.JOB_EXPIRED_ERROR_MESSAGE
    assert fresh_job_row["status"] == "waiting-file"
    assert audit_log_row["from_state"] == "running"
    assert audit_log_row["to_state"] == "failed"
    assert audit_log_row["transition_reason"] == "mark_failed"
    assert audit_metadata["sweeper"] is True
    assert audit_metadata["stale_status"] == "running"
    assert audit_metadata["error_code"] == stale_job_sweeper.JOB_EXPIRED_ERROR_CODE


def test_should_skip_duplicate_beat_firing_with_the_real_periodic_redis_lock(
    worker_contract_environment: None,
) -> None:
    stale_job_sweeper, _ = _load_worker_modules()

    first_result = stale_job_sweeper.expire_stale_jobs()
    second_result = stale_job_sweeper.expire_stale_jobs()

    assert first_result == {"status": "success", "expired": 0, "skipped": 0}
    assert second_result == {
        "status": "skipped",
        "reason": "duplicate Beat firing",
    }


def test_should_record_retry_transition_through_sync_state_machine(
    worker_contract_environment: None,
) -> None:
    from shared.core.state_machine.service_sync import SyncStateMachineService
    from shared.core.database_sync import get_sync_db_context
    from shared.services.redis.redis_sync_service import SyncRedisServiceFactory

    job_id = f"job_retry_{uuid4().hex[:12]}"
    user_id = f"worker-user-{uuid4().hex[:12]}"
    _, engine = _load_worker_modules()

    with engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        insert_contract_job(
            connection,
            job_id=job_id,
            user_id=user_id,
            status="failed",
            source_type="file",
            webhook_enabled=False,
            job_metadata=_build_file_job_metadata(),
            error_code="TRANSIENT",
            error_message="temporary failure",
        )

    redis_service = SyncRedisServiceFactory.get_service()
    state_machine = SyncStateMachineService(redis_service=redis_service)

    with get_sync_db_context() as db:
        did_retry = state_machine.handle_retry(
            db,
            job_id,
            retry_metadata={"worker": "contract"},
        )

    assert did_retry is True

    with engine.begin() as connection:
        job_row = (
            connection.execute(
                text(
                    """
                    SELECT status
                    FROM jobs
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id},
            )
            .mappings()
            .one()
        )
        audit_log_row = (
            connection.execute(
                text(
                    """
                    SELECT from_state, to_state, transition_reason, operator_type, transition_metadata
                    FROM job_state_audit_logs
                    WHERE job_id = :job_id
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ),
                {"job_id": job_id},
            )
            .mappings()
            .one()
        )

    audit_metadata = dict(audit_log_row["transition_metadata"])
    progress = redis_service.hgetall(f"task:{job_id}:progress")

    assert job_row["status"] == "pending"
    assert audit_log_row["from_state"] == "failed"
    assert audit_log_row["to_state"] == "pending"
    assert audit_log_row["transition_reason"] == "retry_transition"
    assert audit_log_row["operator_type"] == "retry"
    assert audit_metadata["worker"] == "contract"
    assert audit_metadata["retry_reason"] == "task_retry"
    assert audit_metadata["retry_count"] == 1
    assert audit_metadata["retry_timestamp"]
    assert progress["status"] == "pending"
    assert progress["worker"] == "contract"
    assert progress["retry_count"] == 1


def test_should_expose_sync_state_machine_rejection_reason(
    worker_contract_environment: None,
) -> None:
    from shared.core.database_sync import get_sync_db_context
    from shared.core.state_machine.service_sync import SyncStateMachineService
    from shared.core.state_machine.states import JobStatus
    from shared.services.redis.redis_sync_service import SyncRedisServiceFactory

    job_id = f"job_outcome_{uuid4().hex[:12]}"
    user_id = f"worker-user-{uuid4().hex[:12]}"
    _, engine = _load_worker_modules()

    with engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        insert_contract_job(
            connection,
            job_id=job_id,
            user_id=user_id,
            status="done",
            source_type="file",
            webhook_enabled=False,
            job_metadata=_build_file_job_metadata(),
        )

    state_machine = SyncStateMachineService(
        redis_service=SyncRedisServiceFactory.get_service()
    )

    with get_sync_db_context() as db:
        outcome = state_machine.transition_outcome(
            db,
            job_id,
            JobStatus.RUNNING.value,
            transition_reason="contract_invalid_transition",
        )

    assert outcome.succeeded is False
    assert outcome.reason == "invalid_transition"
    assert outcome.from_state == "done"
    assert outcome.to_state == "running"
    assert outcome.attempts == 1
