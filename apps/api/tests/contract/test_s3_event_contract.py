import importlib
import json
import socket
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import cast
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from tests.support.contract_database import ContractDatabase


def _build_s3_event_payload(job_id: str) -> dict[str, object]:
    return {
        "Records": [
            {
                "eventVersion": "2.1",
                "eventSource": "aws:s3",
                "awsRegion": "us-west-1",
                "eventTime": "2026-04-26T00:00:00.000Z",
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": "knowhere-test-uploads"},
                    "object": {"key": f"uploads/{job_id}.pdf", "size": 256},
                },
            }
        ]
    }


async def _insert_waiting_file_job(
    *, job_type: str = "document_ingestion"
) -> tuple[str, str]:
    user_id = f"contract-s3-user-{uuid4().hex[:12]}"
    job_id = f"job_{uuid4().hex[:12]}"

    await ContractDatabase.insert_user(user_id=user_id)
    await ContractDatabase.insert_job(
        job_id=job_id,
        user_id=user_id,
        status="waiting-file",
        job_type=job_type,
        source_type="file",
        s3_key=f"uploads/{job_id}.pdf",
    )

    return user_id, job_id


@pytest.mark.asyncio
async def test_should_acknowledge_an_sns_subscription_confirmation_request(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
) -> None:
    async with api_client_factory() as api_client:
        response = await api_client.get(
            "/api/v1/internal/s3-events",
            headers={"x-amz-sns-message-type": "SubscriptionConfirmation"},
        )

    assert response.status_code == 200
    assert response.json() == {"message": "SNS subscription confirmed"}


@pytest.mark.asyncio
async def test_should_accept_a_direct_upload_complete_event_advance_the_waiting_job_and_start_workflow_handoff(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    workflow_calls: list[dict[str, str]] = []
    user_id: str = ""
    job_id: str = ""

    class FakeDocumentIngestionWorkerDispatcher:
        async def start_uploaded_file_parse(
            self,
            *,
            job_id: str,
            user_id: str,
        ) -> str:
            workflow_calls.append(
                {
                    "job_id": job_id,
                    "user_id": user_id,
                }
            )
            return "contract-task-id"

    async with api_client_factory() as api_client:
        user_id, job_id = await _insert_waiting_file_job()
        handoff_service = importlib.import_module(
            "app.services.document_ingestion.handoff_service"
        )
        monkeypatch.setattr(
            handoff_service,
            "DocumentIngestionWorkerDispatcher",
            FakeDocumentIngestionWorkerDispatcher,
        )
        response = await api_client.post(
            "/api/v1/internal/s3-events",
            json=_build_s3_event_payload(job_id),
        )

    assert response.status_code == 200
    assert response.json() == {"message": "Event handled successfully"}

    job_row = await ContractDatabase.fetch_job(job_id)

    assert job_row is not None
    assert job_row["status"] == "pending"
    assert workflow_calls == [
        {
            "job_id": job_id,
            "user_id": user_id,
        }
    ]


@pytest.mark.asyncio
async def test_should_accept_a_pre_rename_waiting_file_job_type_during_upload_handoff(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    workflow_calls: list[dict[str, str]] = []
    user_id: str = ""
    job_id: str = ""

    class FakeDocumentIngestionWorkerDispatcher:
        async def start_uploaded_file_parse(
            self,
            *,
            job_id: str,
            user_id: str,
        ) -> str:
            workflow_calls.append(
                {
                    "job_id": job_id,
                    "user_id": user_id,
                }
            )
            return "contract-task-id"

    async with api_client_factory() as api_client:
        user_id, job_id = await _insert_waiting_file_job(job_type="kb_management")
        handoff_service = importlib.import_module(
            "app.services.document_ingestion.handoff_service"
        )
        monkeypatch.setattr(
            handoff_service,
            "DocumentIngestionWorkerDispatcher",
            FakeDocumentIngestionWorkerDispatcher,
        )
        response = await api_client.post(
            "/api/v1/internal/s3-events",
            json=_build_s3_event_payload(job_id),
        )

    assert response.status_code == 200
    assert response.json() == {"message": "Event handled successfully"}

    job_row = await ContractDatabase.fetch_job(job_id)

    assert job_row is not None
    assert job_row["status"] == "pending"
    assert workflow_calls == [
        {
            "job_id": job_id,
            "user_id": user_id,
        }
    ]


@pytest.mark.asyncio
async def test_should_accept_an_sns_wrapped_upload_complete_event_and_advance_a_waiting_file_job(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    job_id: str = ""

    class FakeDocumentIngestionWorkerDispatcher:
        async def start_uploaded_file_parse(
            self,
            *,
            job_id: str,
            user_id: str,
        ) -> str:
            return "contract-task-id"

    async with api_client_factory() as api_client:
        _, job_id = await _insert_waiting_file_job()
        handoff_service = importlib.import_module(
            "app.services.document_ingestion.handoff_service"
        )
        monkeypatch.setattr(
            handoff_service,
            "DocumentIngestionWorkerDispatcher",
            FakeDocumentIngestionWorkerDispatcher,
        )
        response = await api_client.post(
            "/api/v1/internal/s3-events",
            content=json.dumps(
                {
                    "Type": "Notification",
                    "Message": json.dumps(_build_s3_event_payload(job_id)),
                }
            ).encode("utf-8"),
            headers={"x-amz-sns-message-type": "Notification"},
        )

    assert response.status_code == 200
    assert response.json() == {"message": "Event handled successfully"}

    job_row = await ContractDatabase.fetch_job(job_id)

    assert job_row is not None
    assert job_row["status"] == "pending"


@pytest.mark.asyncio
async def test_should_reject_an_sns_subscription_confirmation_url_that_resolves_to_a_private_host(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    contacted_urls: list[str] = []

    def resolve_private_address(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    class _UnexpectedSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_UnexpectedSession":
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: object,
        ) -> None:
            return None

        def get(self, url: str, *args: object, **kwargs: object) -> object:
            contacted_urls.append(url)
            raise AssertionError("private SNS confirmation URL should not be requested")

    async with api_client_factory() as api_client:
        monkeypatch.setattr(socket, "getaddrinfo", resolve_private_address)
        pinned_http_module = importlib.import_module(
            "shared.services.http.pinned_outbound"
        )
        monkeypatch.setattr(
            pinned_http_module.aiohttp,
            "ClientSession",
            _UnexpectedSession,
        )
        response = await api_client.post(
            "/api/v1/internal/s3-events",
            content=json.dumps(
                {
                    "Type": "SubscriptionConfirmation",
                    "SubscribeURL": "https://sns.example.test/confirm",
                }
            ).encode("utf-8"),
            headers={"x-amz-sns-message-type": "SubscriptionConfirmation"},
        )

    assert response.status_code == 200
    assert response.json() == {"message": "SNS subscription confirmation failed"}
    assert contacted_urls == []


@pytest.mark.asyncio
async def test_should_return_ok_for_a_malformed_event_payload_without_triggering_retries(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
) -> None:
    async with api_client_factory() as api_client:
        response = await api_client.post(
            "/api/v1/internal/s3-events",
            content=b"{this-is-not-valid-json",
        )

    assert response.status_code == 200
    response_json = cast(dict[str, object], response.json())
    assert response_json["message"] == "Event handled successfully"
