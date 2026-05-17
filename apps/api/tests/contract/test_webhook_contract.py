import importlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from tests.support.contract_database import ContractDatabase


async def _insert_webhook_job(
    *,
    user_id: str,
    status: str,
    webhook_url: str | None = "https://hooks.contract.test/jobs",
) -> str:
    job_id = f"job_{uuid4().hex[:12]}"
    await ContractDatabase.insert_job(
        job_id=job_id,
        user_id=user_id,
        status=status,
        source_type="file",
        webhook_url=webhook_url,
    )
    return job_id


async def _insert_webhook_event(
    *,
    job_id: str,
    target_url: str = "https://hooks.contract.test/jobs",
) -> str:
    event_id = str(uuid4())
    await ContractDatabase.insert_webhook_event(
        event_id=event_id,
        job_id=job_id,
        target_url=target_url,
        payload={"job_id": job_id, "status": "done"},
    )
    return event_id


@pytest.mark.asyncio
async def test_should_return_paginated_webhook_delivery_logs_for_the_authenticated_user(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        first_job_id = await _insert_webhook_job(user_id="local-dev-user", status="done")
        second_job_id = await _insert_webhook_job(user_id="local-dev-user", status="done")
        other_user_id = f"contract-webhook-user-{uuid4().hex[:12]}"

        await ContractDatabase.insert_user(user_id=other_user_id)
        other_job_id = await _insert_webhook_job(user_id=other_user_id, status="done")

        await ContractDatabase.insert_webhook_log(
            log_id=str(uuid4()),
            job_id=first_job_id,
            event_id=None,
            webhook_url="https://hooks.contract.test/jobs/first",
            attempt_number=1,
            response_status_code=202,
            response_body="accepted",
            duration_ms=45,
        )
        await ContractDatabase.insert_webhook_log(
            log_id=str(uuid4()),
            job_id=second_job_id,
            event_id=None,
            webhook_url="https://hooks.contract.test/jobs/second",
            attempt_number=2,
            response_status_code=500,
            error_message="timeout",
            duration_ms=120,
        )
        await ContractDatabase.insert_webhook_log(
            log_id=str(uuid4()),
            job_id=other_job_id,
            event_id=None,
            webhook_url="https://hooks.contract.test/jobs/other",
            attempt_number=1,
            response_status_code=200,
            duration_ms=30,
        )

        response = await api_client.get(
            "/api/v1/webhooks/logs",
            params={"page": 1, "page_size": 10},
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    logs = cast(list[dict[str, object]], response_json["logs"])
    returned_job_ids = {cast(str, log["job_id"]) for log in logs}

    assert response_json["total"] == 2
    assert response_json["page"] == 1
    assert response_json["page_size"] == 10
    assert len(logs) == 2
    assert returned_job_ids == {first_job_id, second_job_id}
    assert other_job_id not in returned_job_ids


@pytest.mark.asyncio
async def test_should_filter_webhook_logs_by_job_id(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        filtered_job_id = await _insert_webhook_job(user_id="local-dev-user", status="done")
        ignored_job_id = await _insert_webhook_job(user_id="local-dev-user", status="done")

        await ContractDatabase.insert_webhook_log(
            log_id=str(uuid4()),
            job_id=filtered_job_id,
            event_id=None,
            webhook_url="https://hooks.contract.test/jobs/filtered",
            attempt_number=1,
            response_status_code=200,
            duration_ms=15,
        )
        await ContractDatabase.insert_webhook_log(
            log_id=str(uuid4()),
            job_id=ignored_job_id,
            event_id=None,
            webhook_url="https://hooks.contract.test/jobs/ignored",
            attempt_number=1,
            response_status_code=500,
            error_message="boom",
            duration_ms=90,
        )

        response = await api_client.get(
            "/api/v1/webhooks/logs",
            params={"job_id": filtered_job_id},
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    logs = cast(list[dict[str, object]], response_json["logs"])

    assert response_json["total"] == 1
    assert len(logs) == 1
    assert logs[0]["job_id"] == filtered_job_id
    assert logs[0]["webhook_url"] == "https://hooks.contract.test/jobs/filtered"


@pytest.mark.asyncio
async def test_should_trigger_a_webhook_for_an_owned_terminal_job_with_a_matching_event(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    job_id: str = ""
    event_id: str = ""

    class FakeDispatcher:
        async def send_manual_webhook(self, db, event):
            assert event.id == event_id
            assert event.job_id == job_id
            return SimpleNamespace(
                success=True,
                status_code=202,
                duration_ms=118,
                error_message=None,
            )

    async with developer_api_client_factory() as api_client:
        job_id = await _insert_webhook_job(user_id="local-dev-user", status="done")
        event_id = await _insert_webhook_event(job_id=job_id)
        webhook_module = importlib.import_module("app.api.v1.routes.webhook")
        monkeypatch.setattr(
            webhook_module,
            "get_webhook_dispatcher",
            lambda: FakeDispatcher(),
        )
        response = await api_client.post(
            "/api/v1/webhooks/trigger",
            json={"job_id": job_id},
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "status_code": 202,
        "response_body": None,
        "duration_ms": 118,
        "delivery_id": None,
        "error_message": None,
    }


@pytest.mark.asyncio
async def test_should_return_invalid_argument_when_triggering_a_non_terminal_job(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    job_id: str = ""

    async with developer_api_client_factory() as api_client:
        job_id = await _insert_webhook_job(user_id="local-dev-user", status="pending")
        response = await api_client.post(
            "/api/v1/webhooks/trigger",
            json={"job_id": job_id},
        )

    assert response.status_code == 400
    assert response.headers["x-request-id"]

    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])
    details = cast(dict[str, object], error["details"])
    violations = cast(list[dict[str, object]], details["violations"])

    assert response_json["success"] is False
    assert error["code"] == "INVALID_ARGUMENT"
    assert (
        error["message"]
        == "Job must be in terminal state to trigger webhook. Current status: pending"
    )
    assert violations == [
        {
            "field": "job_id",
            "description": "Job status is 'pending', expected 'done' or 'failed'",
        }
    ]


@pytest.mark.asyncio
async def test_should_return_not_found_when_the_job_has_no_webhook_event(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    job_id: str = ""

    async with developer_api_client_factory() as api_client:
        job_id = await _insert_webhook_job(user_id="local-dev-user", status="done")
        response = await api_client.post(
            "/api/v1/webhooks/trigger",
            json={"job_id": job_id},
        )

    assert response.status_code == 404
    assert response.headers["x-request-id"]

    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "NOT_FOUND"
    assert error["message"] == "WebhookEvent not found"
    assert error["details"] == {
        "resource": "WebhookEvent",
        "id": job_id,
    }


@pytest.mark.asyncio
async def test_should_forbid_triggering_a_webhook_across_an_ownership_boundary(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    other_user_id = f"contract-webhook-owner-{uuid4().hex[:12]}"
    other_api_key = f"sk_contract_{uuid4().hex[:24]}"

    async with developer_api_client_factory() as api_client:
        await ContractDatabase.insert_authenticated_user(
            user_id=other_user_id,
            api_key=other_api_key,
        )
        job_id = await _insert_webhook_job(user_id=other_user_id, status="done")
        await _insert_webhook_event(job_id=job_id)
        response = await api_client.post(
            "/api/v1/webhooks/trigger",
            json={"job_id": job_id},
        )

    assert response.status_code == 403
    assert response.headers["x-request-id"]

    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "PERMISSION_DENIED"
    assert error["message"] == "You don't have permission to trigger webhook for this job"
    assert error["details"] == {"required_permission": "job:webhook:trigger"}
