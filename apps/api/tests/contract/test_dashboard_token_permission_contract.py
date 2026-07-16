from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import cast
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from shared.testing.contract_runtime import seed_contract_developer
from tests.support.contract_database import ContractDatabase
from tests.support.dashboard_jwt import use_dashboard_jwks_token


async def _seed_dashboard_user() -> str:
    developer_profile = await seed_contract_developer()
    return cast(str, developer_profile["user_id"])


async def _fetch_document_status(document_id: str) -> str:
    row = await ContractDatabase.fetch_one(
        """
        SELECT status
        FROM documents
        WHERE document_id = :document_id
        """,
        {"document_id": document_id},
    )
    if row is None:
        raise AssertionError(f"document not found: {document_id}")
    return cast(str, row["status"])


async def _count_user_jobs(user_id: str) -> int:
    row = await ContractDatabase.fetch_one(
        """
        SELECT COUNT(*) AS total
        FROM jobs
        WHERE user_id = :user_id
        """,
        {"user_id": user_id},
    )
    if row is None:
        return 0
    return cast(int, row["total"])


def _assert_read_only_error(response_json: dict[str, object]) -> None:
    error = cast(dict[str, object], response_json["error"])
    assert response_json["success"] is False
    assert error["code"] == "PERMISSION_DENIED"
    assert error["message"] == "This token is read only."
    assert error["details"] == {"required_permission": "full_access"}


@pytest.mark.asyncio
async def test_read_only_dashboard_token_can_read_but_cannot_parse_or_archive(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    document_id = f"doc_permission_{uuid4().hex[:12]}"
    payload: dict[str, str] = {
        "namespace": "contract-permission",
        "source_type": "file",
        "file_name": "contract-read-only.pdf",
        "data_id": f"contract-read-only-{uuid4().hex[:12]}",
    }

    async with api_client_factory() as api_client:
        user_id = await _seed_dashboard_user()
        await ContractDatabase.insert_document(
            document_id=document_id,
            user_id=user_id,
            namespace="contract-permission",
        )

        with use_dashboard_jwks_token(
            api_client,
            monkeypatch,
            user_id=user_id,
            permission="read_only",
        ):
            list_jobs_response = await api_client.get("/api/v1/jobs")
            get_document_response = await api_client.get(
                f"/api/v1/documents/{document_id}"
            )
            create_job_response = await api_client.post("/api/v1/jobs", json=payload)
            archive_document_response = await api_client.post(
                f"/api/v1/documents/{document_id}/archive"
            )

    assert list_jobs_response.status_code == 200
    assert get_document_response.status_code == 200

    assert create_job_response.status_code == 403
    _assert_read_only_error(cast(dict[str, object], create_job_response.json()))

    assert archive_document_response.status_code == 403
    _assert_read_only_error(cast(dict[str, object], archive_document_response.json()))

    assert await _count_user_jobs(user_id) == 0
    assert await _fetch_document_status(document_id) == "active"


@pytest.mark.asyncio
async def test_dashboard_token_without_permission_claim_keeps_full_access(
    api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    monkeypatch: MonkeyPatch,
) -> None:
    payload: dict[str, str] = {
        "namespace": "contract-permission",
        "source_type": "file",
        "file_name": "contract-full-access.pdf",
        "data_id": f"contract-full-access-{uuid4().hex[:12]}",
    }

    async with api_client_factory() as api_client:
        user_id = await _seed_dashboard_user()

        with use_dashboard_jwks_token(
            api_client,
            monkeypatch,
            user_id=user_id,
        ):
            response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 200
    response_json = cast(dict[str, object], response.json())
    assert cast(str, response_json["job_id"]).startswith("job_")
    assert response_json["status"] == "waiting-file"
