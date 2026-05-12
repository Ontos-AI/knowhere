from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from pytest import MonkeyPatch
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from support.contract_database import insert_contract_job, insert_contract_user


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _build_file_job_metadata() -> dict[str, str]:
    return {
        "document_id": f"doc_{uuid4().hex[:12]}",
        "namespace": "worker-contract",
        "source_type": "file",
    }


def _insert_webhook_event(
    connection: Connection,
    *,
    event_id: str,
    job_id: str,
    target_url: str,
    status: str,
    attempts: int,
    created_at: datetime,
    updated_at: datetime | None = None,
    qstash_message_id: str | None = None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO webhook_events (
                id,
                job_id,
                target_url,
                payload,
                status,
                attempts,
                next_retry_at,
                qstash_message_id,
                created_at,
                updated_at
            ) VALUES (
                :id,
                :job_id,
                :target_url,
                CAST(:payload AS JSON),
                :status,
                :attempts,
                :next_retry_at,
                :qstash_message_id,
                :created_at,
                :updated_at
            )
            """
        ),
        {
            "id": event_id,
            "job_id": job_id,
            "target_url": target_url,
            "payload": json.dumps({"event": "job.failed", "job_id": job_id}),
            "status": status,
            "attempts": attempts,
            "next_retry_at": None,
            "qstash_message_id": qstash_message_id,
            "created_at": created_at,
            "updated_at": updated_at or created_at,
        },
    )


def _load_worker_modules() -> tuple[Any, Any, Engine]:
    import app.core.tasks.webhook_tasks as webhook_tasks
    from shared.core.database_sync import get_sync_engine
    from shared.services.webhook import qstash_publisher

    return webhook_tasks, qstash_publisher, get_sync_engine()


def test_should_republish_only_orphaned_pending_webhook_events_and_persist_qstash_delivery_state(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
) -> None:
    webhook_tasks, qstash_publisher, engine = _load_worker_modules()

    user_id = f"worker-user-{uuid4().hex[:12]}"
    target_url = "https://hooks.contract.test/worker"
    orphaned_job_id = f"job_orphaned_{uuid4().hex[:12]}"
    recent_job_id = f"job_recent_{uuid4().hex[:12]}"
    retried_job_id = f"job_retried_{uuid4().hex[:12]}"
    terminal_job_id = f"job_terminal_{uuid4().hex[:12]}"
    orphaned_event_id = str(uuid4())
    recent_event_id = str(uuid4())
    retried_event_id = str(uuid4())
    terminal_event_id = str(uuid4())
    published_message_ids: list[str] = []
    published_calls: list[dict[str, Any]] = []

    publisher = qstash_publisher.QStashWebhookPublisher()

    class FakeMessageClient:
        def publish(self, **kwargs: Any) -> SimpleNamespace:
            published_calls.append(kwargs)
            message_id = f"msg_{kwargs['headers']['X-Knowhere-Event-ID']}"
            published_message_ids.append(message_id)
            return SimpleNamespace(message_id=message_id)

    monkeypatch.setattr(
        qstash_publisher,
        "get_qstash_webhook_publisher",
        lambda: publisher,
    )
    monkeypatch.setattr(
        qstash_publisher,
        "validate_http_url_and_resolve_ip",
        lambda *args, **kwargs: SimpleNamespace(
            is_valid=True,
            error_message=None,
            validated_ip="93.184.216.34",
            hostname="hooks.contract.test",
        ),
    )
    monkeypatch.setattr(
        publisher,
        "_get_client",
        lambda: SimpleNamespace(message=FakeMessageClient()),
    )

    now = _utc_now()
    with engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        for job_id in (
            orphaned_job_id,
            recent_job_id,
            retried_job_id,
            terminal_job_id,
        ):
            insert_contract_job(
                connection,
                job_id=job_id,
                user_id=user_id,
                status="done",
                source_type="file",
                webhook_url=target_url,
                webhook_enabled=True,
                job_metadata=_build_file_job_metadata(),
                billing_status="charged",
            )

        _insert_webhook_event(
            connection,
            event_id=orphaned_event_id,
            job_id=orphaned_job_id,
            target_url=target_url,
            status="pending",
            attempts=0,
            created_at=now - timedelta(minutes=10),
        )
        _insert_webhook_event(
            connection,
            event_id=recent_event_id,
            job_id=recent_job_id,
            target_url=target_url,
            status="pending",
            attempts=0,
            created_at=now - timedelta(minutes=1),
        )
        _insert_webhook_event(
            connection,
            event_id=retried_event_id,
            job_id=retried_job_id,
            target_url=target_url,
            status="pending",
            attempts=1,
            created_at=now - timedelta(minutes=10),
        )
        _insert_webhook_event(
            connection,
            event_id=terminal_event_id,
            job_id=terminal_job_id,
            target_url=target_url,
            status="delivered",
            attempts=0,
            created_at=now - timedelta(minutes=10),
        )

    result = webhook_tasks.recover_orphaned_webhooks()

    assert result == {
        "status": "success",
        "recovered": 1,
        "provider": "qstash",
    }
    assert published_message_ids == [f"msg_{orphaned_event_id}"]
    assert published_calls[0]["deduplication_id"] == orphaned_event_id
    assert published_calls[0]["label"] == "knowhere-webhook"

    with engine.begin() as connection:
        event_rows = connection.execute(
            text(
                """
                SELECT id, status, attempts, qstash_message_id
                FROM webhook_events
                WHERE id IN (
                    :orphaned_event_id,
                    :recent_event_id,
                    :retried_event_id,
                    :terminal_event_id
                )
                ORDER BY id
                """
            ),
            {
                "orphaned_event_id": orphaned_event_id,
                "recent_event_id": recent_event_id,
                "retried_event_id": retried_event_id,
                "terminal_event_id": terminal_event_id,
            },
        ).mappings()
        secrets_count_row = connection.execute(
            text(
                """
                SELECT COUNT(*) AS secrets_count
                FROM webhook_secrets
                WHERE user_id = :user_id AND endpoint = :endpoint
                """
            ),
            {
                "user_id": user_id,
                "endpoint": target_url,
            },
        ).mappings().one()

    events_by_id = {row["id"]: dict(row) for row in event_rows}

    assert events_by_id[orphaned_event_id] == {
        "id": orphaned_event_id,
        "status": "delivering",
        "attempts": 0,
        "qstash_message_id": f"msg_{orphaned_event_id}",
    }
    assert events_by_id[recent_event_id] == {
        "id": recent_event_id,
        "status": "pending",
        "attempts": 0,
        "qstash_message_id": None,
    }
    assert events_by_id[retried_event_id] == {
        "id": retried_event_id,
        "status": "pending",
        "attempts": 1,
        "qstash_message_id": None,
    }
    assert events_by_id[terminal_event_id] == {
        "id": terminal_event_id,
        "status": "delivered",
        "attempts": 0,
        "qstash_message_id": None,
    }
    assert secrets_count_row["secrets_count"] == 1


def test_should_reconcile_stale_delivering_webhook_events_from_qstash_logs(
    worker_contract_environment: None,
    monkeypatch: MonkeyPatch,
) -> None:
    webhook_tasks, qstash_publisher, engine = _load_worker_modules()

    user_id = f"worker-user-{uuid4().hex[:12]}"
    target_url = "https://hooks.contract.test/worker"
    job_id = f"job_stale_{uuid4().hex[:12]}"
    event_id = str(uuid4())
    qstash_message_id = f"msg_{event_id}"

    class FakePublisher:
        def publish_event(self, event_id: str) -> None:
            raise AssertionError(f"stale delivering event should not republish: {event_id}")

        def get_terminal_delivery_status(
            self,
            message_id: str,
        ) -> Any:
            assert message_id == qstash_message_id
            return qstash_publisher.QStashDeliveryStatus(
                status="delivered",
                response_status_code=204,
                response_body="",
                error_message=None,
            )

    monkeypatch.setattr(
        qstash_publisher,
        "get_qstash_webhook_publisher",
        lambda: FakePublisher(),
    )

    now = _utc_now()
    with engine.begin() as connection:
        insert_contract_user(connection, user_id=user_id)
        insert_contract_job(
            connection,
            job_id=job_id,
            user_id=user_id,
            status="done",
            source_type="file",
            webhook_url=target_url,
            webhook_enabled=True,
            job_metadata=_build_file_job_metadata(),
            billing_status="charged",
        )
        _insert_webhook_event(
            connection,
            event_id=event_id,
            job_id=job_id,
            target_url=target_url,
            status="delivering",
            attempts=2,
            created_at=now - timedelta(minutes=20),
            updated_at=now - timedelta(minutes=10),
            qstash_message_id=qstash_message_id,
        )

    result = webhook_tasks.recover_orphaned_webhooks()

    assert result == {
        "status": "success",
        "recovered": 0,
        "provider": "qstash",
        "reconciled": 1,
    }

    with engine.begin() as connection:
        event_row = (
            connection.execute(
                text(
                    """
                    SELECT id, status, attempts, qstash_message_id
                    FROM webhook_events
                    WHERE id = :event_id
                    """
                ),
                {"event_id": event_id},
            )
            .mappings()
            .one()
        )
        log_row = (
            connection.execute(
                text(
                    """
                    SELECT
                        job_id,
                        event_id,
                        webhook_url,
                        attempt_number,
                        response_status_code,
                        response_body,
                        error_message,
                        delivery_provider,
                        qstash_message_id
                    FROM webhook_logs
                    WHERE event_id = :event_id
                    """
                ),
                {"event_id": event_id},
            )
            .mappings()
            .one()
        )

    assert dict(event_row) == {
        "id": event_id,
        "status": "delivered",
        "attempts": 2,
        "qstash_message_id": qstash_message_id,
    }
    assert dict(log_row) == {
        "job_id": job_id,
        "event_id": event_id,
        "webhook_url": target_url,
        "attempt_number": 2,
        "response_status_code": 204,
        "response_body": None,
        "error_message": None,
        "delivery_provider": "qstash",
        "qstash_message_id": qstash_message_id,
    }


def test_should_skip_duplicate_beat_firing_for_webhook_recovery(
    worker_contract_environment: None,
) -> None:
    webhook_tasks, _, _ = _load_worker_modules()

    first_result = webhook_tasks.recover_orphaned_webhooks()
    second_result = webhook_tasks.recover_orphaned_webhooks()

    assert first_result == {
        "status": "success",
        "recovered": 0,
        "provider": "qstash",
    }
    assert second_result == {
        "status": "skipped",
        "reason": "duplicate Beat firing",
    }
