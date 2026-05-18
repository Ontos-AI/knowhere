import importlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import cast
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from tests.support.contract_database import ContractDatabase


async def _insert_qstash_event(
    *,
    status: str = "pending",
    attempts: int = 0,
    qstash_message_id: str | None = None,
) -> tuple[str, str]:
    user_id = f"contract-qstash-user-{uuid4().hex[:12]}"
    job_id = f"job_{uuid4().hex[:12]}"
    event_id = str(uuid4())

    await ContractDatabase.insert_user(user_id=user_id)
    await ContractDatabase.insert_job(
        job_id=job_id,
        user_id=user_id,
        status="done",
        source_type="file",
        webhook_url="https://hooks.contract.test/qstash",
    )
    await ContractDatabase.insert_webhook_event(
        event_id=event_id,
        job_id=job_id,
        target_url="https://hooks.contract.test/qstash",
        payload={"job_id": job_id, "status": "done"},
        status=status,
        attempts=attempts,
        qstash_message_id=qstash_message_id,
    )

    return job_id, event_id


@pytest.mark.asyncio
async def test_should_return_unauthorized_for_an_invalid_qstash_callback_signature(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    async with api_client_factory() as api_client:
        qstash_module = importlib.import_module(
            "app.services.webhook.qstash_callback_service"
        )
        monkeypatch.setattr(qstash_module, "verify_qstash_signature", lambda *args: False)
        response = await api_client.post(
            "/api/v1/webhooks/qstash/callback",
            json={"status": 200},
            headers={"upstash-signature": "invalid-signature"},
        )

    assert response.status_code == 401
    assert response.text == "Invalid signature"


@pytest.mark.asyncio
async def test_should_mark_the_matching_event_delivered_and_persist_a_webhook_log_on_success_callback(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    job_id: str = ""
    event_id: str = ""

    async with api_client_factory() as api_client:
        job_id, event_id = await _insert_qstash_event()
        qstash_module = importlib.import_module(
            "app.services.webhook.qstash_callback_service"
        )
        monkeypatch.setattr(qstash_module, "verify_qstash_signature", lambda *args: True)
        response = await api_client.post(
            "/api/v1/webhooks/qstash/callback",
            json={
                "status": 202,
                "body": "accepted",
                "retried": 1,
                "sourceMessageId": "qstash-message-success",
                "sourceHeader": {"X-Knowhere-Event-Id": event_id},
            },
            headers={"upstash-signature": "contract-valid"},
        )

    assert response.status_code == 200
    assert response.text == "OK"

    event_row = await ContractDatabase.fetch_webhook_event(event_id)
    log_rows = await ContractDatabase.fetch_all(
        """
        SELECT
            job_id,
            event_id,
            attempt_number,
            response_status_code,
            response_body,
            error_message,
            qstash_message_id
        FROM webhook_logs
        WHERE event_id = :event_id
        """,
        {"event_id": event_id},
    )

    assert event_row is not None
    assert event_row["status"] == "delivered"
    assert event_row["attempts"] == 2

    assert len(log_rows) == 1
    assert log_rows[0] == {
        "job_id": job_id,
        "event_id": event_id,
        "attempt_number": 2,
        "response_status_code": 202,
        "response_body": "accepted",
        "error_message": None,
        "qstash_message_id": "qstash-message-success",
    }


@pytest.mark.asyncio
async def test_should_keep_the_matching_event_delivering_for_retry_callback_with_non_success_status(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    job_id: str = ""
    event_id: str = ""

    async with api_client_factory() as api_client:
        job_id, event_id = await _insert_qstash_event(
            status="delivering",
            qstash_message_id="qstash-message-retry",
        )
        qstash_module = importlib.import_module(
            "app.services.webhook.qstash_callback_service"
        )
        monkeypatch.setattr(qstash_module, "verify_qstash_signature", lambda *args: True)
        response = await api_client.post(
            "/api/v1/webhooks/qstash/callback",
            json={
                "status": 503,
                "body": "temporary unavailable",
                "retried": 2,
                "sourceMessageId": "qstash-message-retry",
                "sourceHeader": {"X-Knowhere-Event-Id": event_id},
            },
            headers={"upstash-signature": "contract-valid"},
        )

    assert response.status_code == 200
    assert response.text == "OK"

    event_row = await ContractDatabase.fetch_webhook_event(event_id)
    log_rows = await ContractDatabase.fetch_all(
        """
        SELECT
            job_id,
            event_id,
            attempt_number,
            response_status_code,
            response_body,
            error_message,
            qstash_message_id
        FROM webhook_logs
        WHERE event_id = :event_id
        """,
        {"event_id": event_id},
    )

    assert event_row is not None
    assert event_row["status"] == "delivering"
    assert event_row["attempts"] == 3

    assert len(log_rows) == 1
    assert log_rows[0] == {
        "job_id": job_id,
        "event_id": event_id,
        "attempt_number": 3,
        "response_status_code": 503,
        "response_body": "temporary unavailable",
        "error_message": "temporary unavailable",
        "qstash_message_id": "qstash-message-retry",
    }


@pytest.mark.asyncio
async def test_should_not_downgrade_terminal_event_when_retry_callback_arrives_late(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    job_id: str = ""
    event_id: str = ""

    async with api_client_factory() as api_client:
        job_id, event_id = await _insert_qstash_event(
            status="delivered",
            attempts=4,
            qstash_message_id="qstash-message-late-retry",
        )
        qstash_module = importlib.import_module(
            "app.services.webhook.qstash_callback_service"
        )
        monkeypatch.setattr(qstash_module, "verify_qstash_signature", lambda *args: True)
        response = await api_client.post(
            "/api/v1/webhooks/qstash/callback",
            json={
                "status": 503,
                "body": "late retry callback",
                "retried": 1,
                "sourceMessageId": "qstash-message-late-retry",
                "sourceHeader": {"X-Knowhere-Event-Id": event_id},
            },
            headers={"upstash-signature": "contract-valid"},
        )

    assert response.status_code == 200
    assert response.text == "OK"

    event_row = await ContractDatabase.fetch_webhook_event(event_id)
    log_rows = await ContractDatabase.fetch_all(
        """
        SELECT
            job_id,
            event_id,
            attempt_number,
            response_status_code,
            response_body,
            error_message,
            qstash_message_id
        FROM webhook_logs
        WHERE event_id = :event_id
        """,
        {"event_id": event_id},
    )

    assert event_row is not None
    assert event_row["status"] == "delivered"
    assert event_row["attempts"] == 4

    assert len(log_rows) == 1
    assert log_rows[0] == {
        "job_id": job_id,
        "event_id": event_id,
        "attempt_number": 2,
        "response_status_code": 503,
        "response_body": "late retry callback",
        "error_message": "late retry callback",
        "qstash_message_id": "qstash-message-late-retry",
    }


@pytest.mark.asyncio
async def test_should_mark_the_matching_event_failed_and_persist_the_error_on_failure_callback(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    job_id: str = ""
    event_id: str = ""

    async with api_client_factory() as api_client:
        job_id, event_id = await _insert_qstash_event()
        qstash_module = importlib.import_module(
            "app.services.webhook.qstash_callback_service"
        )
        monkeypatch.setattr(qstash_module, "verify_qstash_signature", lambda *args: True)
        response = await api_client.post(
            "/api/v1/webhooks/qstash/failure",
            json={
                "status": 500,
                "body": "gateway timeout",
                "error": "upstream timeout",
                "retried": 5,
                "maxRetries": 5,
                "sourceMessageId": "qstash-message-failure",
                "sourceHeader": {"x-knowhere-event-id": event_id},
            },
            headers={"upstash-signature": "contract-valid"},
        )

    assert response.status_code == 200
    assert response.text == "OK"

    event_row = await ContractDatabase.fetch_webhook_event(event_id)
    log_rows = await ContractDatabase.fetch_all(
        """
        SELECT
            job_id,
            event_id,
            attempt_number,
            response_status_code,
            response_body,
            error_message,
            qstash_message_id
        FROM webhook_logs
        WHERE event_id = :event_id
        """,
        {"event_id": event_id},
    )

    assert event_row is not None
    assert event_row["status"] == "failed"
    assert event_row["attempts"] == 6

    assert len(log_rows) == 1
    assert log_rows[0] == {
        "job_id": job_id,
        "event_id": event_id,
        "attempt_number": 6,
        "response_status_code": 500,
        "response_body": "gateway timeout",
        "error_message": "upstream timeout",
        "qstash_message_id": "qstash-message-failure",
    }


@pytest.mark.asyncio
async def test_should_return_ok_without_mutating_state_when_the_callback_has_no_correlated_event_id(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    event_id: str = ""

    async with api_client_factory() as api_client:
        _, event_id = await _insert_qstash_event()
        qstash_module = importlib.import_module(
            "app.services.webhook.qstash_callback_service"
        )
        monkeypatch.setattr(qstash_module, "verify_qstash_signature", lambda *args: True)
        response = await api_client.post(
            "/api/v1/webhooks/qstash/callback",
            json={
                "status": 200,
                "body": "accepted",
                "sourceMessageId": "qstash-message-uncorrelated",
            },
            headers={"upstash-signature": "contract-valid"},
        )

    assert response.status_code == 200
    assert response.text == "OK (no event_id)"

    event_row = await ContractDatabase.fetch_webhook_event(event_id)
    log_count_row = await ContractDatabase.fetch_one(
        """
        SELECT COUNT(*) AS count
        FROM webhook_logs
        WHERE event_id = :event_id
        """,
        {"event_id": event_id},
    )

    assert event_row is not None
    assert log_count_row is not None
    assert event_row["status"] == "pending"
    assert event_row["attempts"] == 0
    assert cast(int, log_count_row["count"]) == 0
