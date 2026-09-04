import asyncio
import importlib
import json
import socket
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
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
async def test_should_not_dispatch_a_second_task_for_a_replayed_upload_event(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    workflow_calls: list[dict[str, str]] = []

    class FakeDocumentIngestionWorkerDispatcher:
        async def start_uploaded_file_parse(
            self,
            *,
            job_id: str,
            user_id: str,
        ) -> str:
            workflow_calls.append({"job_id": job_id, "user_id": user_id})
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

        first_response = await api_client.post(
            "/api/v1/internal/s3-events",
            json=_build_s3_event_payload(job_id),
        )
        replay_response = await api_client.post(
            "/api/v1/internal/s3-events",
            json=_build_s3_event_payload(job_id),
        )

    assert first_response.status_code == 200
    assert replay_response.status_code == 200
    assert workflow_calls == [{"job_id": job_id, "user_id": user_id}]


@pytest.mark.asyncio
async def test_should_treat_a_concurrent_upload_handoff_cas_winner_as_a_no_op() -> None:
    from app.services.document_ingestion.handoff_service import (
        DocumentIngestionHandoffService,
    )
    from shared.core.state_machine.transition_outcome import JobTransitionOutcome

    class FakeStateMachine:
        async def transition_outcome(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            return JobTransitionOutcome.rejected(
                job_id="job-race",
                to_state="pending",
                reason="invalid_transition",
                attempts=1,
                from_state="pending",
            )

    class FakeDispatcher:
        async def start_uploaded_file_parse(
            self,
            *,
            job_id: str,
            user_id: str,
        ) -> str:
            del job_id, user_id
            raise AssertionError("CAS loser must not dispatch a duplicate task")

    service = DocumentIngestionHandoffService(
        state_machine=FakeStateMachine(),
        worker_dispatcher=FakeDispatcher(),
    )

    await service.start_uploaded_file_workflow(
        db=cast(object, None),
        job=SimpleNamespace(
            job_id="job-race",
            job_type="document_ingestion",
            status="waiting-file",
        ),
        user_id="contract-user",
        trigger="s3_upload_completed",
    )


@pytest.mark.asyncio
async def test_should_not_dispatch_when_cas_retry_accepts_pending_to_pending() -> None:
    """Reproduce #286: a successful same-state pending → pending still dispatches.

    The state machine treats same-state transitions as valid. After a CAS miss,
    the retry reloads ``pending`` and accepts ``pending → pending``. Handoff
    currently treats any succeeded outcome as dispatch ownership.
    """
    from app.services.document_ingestion.handoff_service import (
        DocumentIngestionHandoffService,
    )
    from shared.core.state_machine.transition_outcome import JobTransitionOutcome

    dispatch_calls: list[str] = []

    class FakeStateMachine:
        async def transition_outcome(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            return JobTransitionOutcome.transitioned(
                job_id="job-race",
                from_state="pending",
                to_state="pending",
                attempts=2,
            )

    class FakeDispatcher:
        async def start_uploaded_file_parse(
            self,
            *,
            job_id: str,
            user_id: str,
        ) -> str:
            del user_id
            dispatch_calls.append(job_id)
            return "contract-task-id"

    service = DocumentIngestionHandoffService(
        state_machine=FakeStateMachine(),
        worker_dispatcher=FakeDispatcher(),
    )

    await service.start_uploaded_file_workflow(
        db=cast(object, None),
        job=SimpleNamespace(
            job_id="job-race",
            job_type="document_ingestion",
            status="waiting-file",
        ),
        user_id="contract-user",
        trigger="manual_upload_completed",
    )

    assert dispatch_calls == []


def _patch_parse_dispatch_and_s3_exists(
    monkeypatch: MonkeyPatch,
    *,
    workflow_calls: list[dict[str, str]],
    verify_s3: Callable[..., object] | None = None,
) -> None:
    async def _fake_start_uploaded_file_parse(
        self: object,
        *,
        job_id: str,
        user_id: str,
    ) -> str:
        del self
        workflow_calls.append({"job_id": job_id, "user_id": user_id})
        return "contract-task-id"

    async def _fake_verify_s3_file_exists(
        self: object,
        s3_key: str,
        bucket: str | None = None,
    ) -> dict[str, object]:
        del self, bucket
        return {"exists": True, "s3_key": s3_key}

    dispatcher_module = importlib.import_module(
        "app.services.document_ingestion.worker_dispatcher"
    )
    file_upload_service_module = importlib.import_module(
        "shared.services.storage.file_upload_service"
    )
    monkeypatch.setattr(
        dispatcher_module.DocumentIngestionWorkerDispatcher,
        "start_uploaded_file_parse",
        _fake_start_uploaded_file_parse,
    )
    monkeypatch.setattr(
        file_upload_service_module.FileUploadService,
        "verify_s3_file_exists",
        verify_s3 or _fake_verify_s3_file_exists,
    )


async def _create_waiting_file_job(
    api_client: AsyncClient,
    *,
    data_id: str,
    file_name: str = "race-upload.pdf",
) -> str:
    create_response = await api_client.post(
        "/api/v1/jobs",
        json={
            "namespace": "contract-jobs",
            "source_type": "file",
            "file_name": file_name,
            "data_id": data_id,
        },
    )
    assert create_response.status_code == 200
    create_response_json: dict[str, object] = create_response.json()
    return cast(str, create_response_json["job_id"])


@pytest.mark.asyncio
async def test_should_dispatch_once_when_s3_notification_races_confirm_upload(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    """Deterministic HTTP interleaving of confirm-upload and the S3 event.

    ``confirm-upload`` loads ``waiting-file`` then waits in S3 existence
    check (the production race window). The ObjectCreated handler completes
    first. Confirm then hands off the stale snapshot; CAS retry accepts
    ``pending → pending`` and currently dispatches a second parse task.
    """
    workflow_calls: list[dict[str, str]] = []
    confirm_entered_verify = asyncio.Event()

    async def _gated_verify_s3_file_exists(
        self: object,
        s3_key: str,
        bucket: str | None = None,
    ) -> dict[str, object]:
        del self, bucket
        confirm_entered_verify.set()
        await asyncio.sleep(0.05)
        return {"exists": True, "s3_key": s3_key}

    async with developer_api_client_factory() as api_client:
        _patch_parse_dispatch_and_s3_exists(
            monkeypatch,
            workflow_calls=workflow_calls,
            verify_s3=_gated_verify_s3_file_exists,
        )
        job_id = await _create_waiting_file_job(
            api_client,
            data_id=f"contract-job-s3-confirm-race-{uuid4().hex[:12]}",
        )

        confirm_task = asyncio.create_task(
            api_client.post(f"/api/v1/jobs/{job_id}/confirm-upload")
        )
        await asyncio.wait_for(confirm_entered_verify.wait(), timeout=5)
        s3_response = await api_client.post(
            "/api/v1/internal/s3-events",
            json=_build_s3_event_payload(job_id),
        )
        confirm_response = await confirm_task

    assert s3_response.status_code == 200
    assert confirm_response.status_code == 200
    assert workflow_calls == [{"job_id": job_id, "user_id": "local-dev-user"}]


@pytest.mark.asyncio
async def test_should_dispatch_once_when_confirm_upload_follows_s3_notification(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    workflow_calls: list[dict[str, str]] = []

    async with developer_api_client_factory() as api_client:
        _patch_parse_dispatch_and_s3_exists(
            monkeypatch,
            workflow_calls=workflow_calls,
        )
        job_id = await _create_waiting_file_job(
            api_client,
            data_id=f"contract-job-s3-then-confirm-{uuid4().hex[:12]}",
        )
        s3_response = await api_client.post(
            "/api/v1/internal/s3-events",
            json=_build_s3_event_payload(job_id),
        )
        confirm_response = await api_client.post(
            f"/api/v1/jobs/{job_id}/confirm-upload"
        )

    assert s3_response.status_code == 200
    assert confirm_response.status_code == 200
    assert workflow_calls == [{"job_id": job_id, "user_id": "local-dev-user"}]


@pytest.mark.asyncio
async def test_should_dispatch_once_when_s3_notification_follows_confirm_upload(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    monkeypatch: MonkeyPatch,
) -> None:
    workflow_calls: list[dict[str, str]] = []

    async with developer_api_client_factory() as api_client:
        _patch_parse_dispatch_and_s3_exists(
            monkeypatch,
            workflow_calls=workflow_calls,
        )
        job_id = await _create_waiting_file_job(
            api_client,
            data_id=f"contract-job-confirm-then-s3-{uuid4().hex[:12]}",
        )
        confirm_response = await api_client.post(
            f"/api/v1/jobs/{job_id}/confirm-upload"
        )
        s3_response = await api_client.post(
            "/api/v1/internal/s3-events",
            json=_build_s3_event_payload(job_id),
        )

    assert s3_response.status_code == 200
    assert confirm_response.status_code == 200
    assert confirm_response.json() == {
        "message": "File upload confirmed; processing started"
    }
    assert workflow_calls == [{"job_id": job_id, "user_id": "local-dev-user"}]


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
async def test_should_confirm_a_configured_localstack_subscription_url_in_self_hosted_runtime(
    monkeypatch: MonkeyPatch,
) -> None:
    contacted_requests: list[dict[str, str]] = []

    class FakeOutboundResponse:
        status = 200

    async def send_fake_pinned_outbound_request(
        *,
        method: str,
        url: str,
        pinned_ip: str,
        timeout_seconds: float,
    ) -> FakeOutboundResponse:
        contacted_requests.append(
            {
                "method": method,
                "url": url,
                "pinned_ip": pinned_ip,
                "timeout_seconds": str(timeout_seconds),
            }
        )
        return FakeOutboundResponse()

    def resolve_localstack_address(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        assert host == "localstack"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.18.0.10", 0))]

    subscription_service = importlib.import_module(
        "app.services.s3_events.subscription_service"
    )

    monkeypatch.setattr(
        subscription_service,
        "settings",
        SimpleNamespace(S3_ENDPOINT_URL="http://localstack:4566"),
    )
    monkeypatch.setattr(socket, "getaddrinfo", resolve_localstack_address)
    monkeypatch.setattr(
        subscription_service,
        "send_pinned_outbound_request",
        send_fake_pinned_outbound_request,
    )

    response = await subscription_service.confirm_sns_subscription(
        "http://localhost.localstack.cloud:4566/"
        "?Action=ConfirmSubscription"
        "&TopicArn=arn:aws:sns:us-west-1:000000000000:test"
        "&Token=contract-token"
    )

    assert response == {"message": "SNS subscription confirmed"}
    assert contacted_requests == [
        {
            "method": "GET",
            "url": (
                "http://localstack:4566/"
                "?Action=ConfirmSubscription"
                "&TopicArn=arn:aws:sns:us-west-1:000000000000:test"
                "&Token=contract-token"
            ),
            "pinned_ip": "172.18.0.10",
            "timeout_seconds": "10",
        }
    ]


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
