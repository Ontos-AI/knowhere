from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import cast
from uuid import uuid4

import pytest
from httpx import AsyncClient, Response

from shared.utils.api_keys import generate_api_key
from tests.support.contract_database import ContractDatabase


@pytest.mark.asyncio
async def test_should_revoke_a_created_api_key_through_http_only(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    create_payload: dict[str, object] = {
        "name": "contract-revocable-key",
        "enabled_modules": ["jobs"],
    }

    async with developer_api_client_factory() as api_client:
        create_response = await api_client.post("/api/v1/auth/create", json=create_payload)
        assert create_response.status_code == 200

        create_response_json = cast(dict[str, object], create_response.json())
        created_api_key = cast(str, create_response_json["api_key"])

        assert created_api_key.startswith("sk_")
        assert create_response_json["name"] == create_payload["name"]
        assert create_response_json["enabled_modules"] == create_payload["enabled_modules"]
        assert create_response_json["expires_at"] is None

        list_response = await api_client.get("/api/v1/auth/list")
        assert list_response.status_code == 200

        list_response_json = cast(dict[str, object], list_response.json())
        api_keys = cast(list[dict[str, object]], list_response_json["api_keys"])
        created_api_key_record = next(
            api_key
            for api_key in api_keys
            if api_key["name"] == create_payload["name"]
        )
        created_api_key_id = cast(str, created_api_key_record["id"])

        revoke_response = await api_client.post(
            "/api/v1/auth/revoke",
            json={"api_key_id": created_api_key_id},
        )
        assert revoke_response.status_code == 200
        assert revoke_response.json() == {"message": "API key revoked"}

        list_after_revoke_response = await api_client.get("/api/v1/auth/list")
        assert list_after_revoke_response.status_code == 200

        list_after_revoke_json = cast(
            dict[str, object], list_after_revoke_response.json()
        )
        api_keys_after_revoke = cast(
            list[dict[str, object]], list_after_revoke_json["api_keys"]
        )

        assert all(
            api_key["id"] != created_api_key_id for api_key in api_keys_after_revoke
        )

        api_client.headers.update({"Authorization": f"Bearer {created_api_key}"})
        rejected_response = await api_client.get("/api/v1/jobs")

    assert rejected_response.status_code == 401
    assert rejected_response.headers["x-request-id"]

    rejected_response_json = cast(dict[str, object], rejected_response.json())
    error = cast(dict[str, object], rejected_response_json["error"])

    assert rejected_response_json["success"] is False
    assert error["code"] == "UNAUTHENTICATED"
    assert error["message"] == "Invalid API Key"
    assert "details" not in error

@pytest.mark.asyncio
async def test_should_return_owned_api_key_metadata(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    create_payload: dict[str, object] = {
        "name": f"contract-detail-{uuid4().hex[:8]}",
        "enabled_modules": ["jobs"],
    }

    async with developer_api_client_factory() as api_client:
        create_response = await api_client.post("/api/v1/auth/create", json=create_payload)
        assert create_response.status_code == 200

        list_response = await api_client.get("/api/v1/auth/list")
        assert list_response.status_code == 200
        list_response_json = cast(dict[str, object], list_response.json())
        api_keys = cast(list[dict[str, object]], list_response_json["api_keys"])
        created_api_key = next(
            api_key
            for api_key in api_keys
            if api_key["name"] == create_payload["name"]
        )
        created_api_key_id = cast(str, created_api_key["id"])

        response = await api_client.get(f"/api/v1/auth/{created_api_key_id}")

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())

    assert response_json["id"] == created_api_key_id
    assert response_json["name"] == create_payload["name"]
    assert response_json["enabled_modules"] == create_payload["enabled_modules"]
    assert response_json["is_active"] is True
    assert response_json["created_at"]
    assert response_json["last_used_at"] is None
    assert response_json["expires_at"] is None


@pytest.mark.asyncio
async def test_should_return_not_found_for_a_missing_api_key_metadata_request(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    missing_api_key_id = f"key_{uuid4().hex[:12]}"

    async with developer_api_client_factory() as api_client:
        response = await api_client.get(f"/api/v1/auth/{missing_api_key_id}")

    assert response.status_code == 404
    assert response.headers["x-request-id"]

    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "NOT_FOUND"
    assert error["message"] == "APIKey not found"
    assert error["details"] == {
        "resource": "APIKey",
        "id": missing_api_key_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "belongs_to_another_user", [False, True], ids=["missing", "another-user"]
)
async def test_should_return_not_found_when_toggling_an_unowned_api_key(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    belongs_to_another_user: bool,
) -> None:
    api_key_id: str = str(uuid4())
    owner_headers: dict[str, str] = {}

    async with developer_api_client_factory() as api_client:
        if belongs_to_another_user:
            owner_key: str = generate_api_key()
            owner_headers = {"Authorization": f"Bearer {owner_key}"}
            await ContractDatabase.insert_authenticated_user(
                user_id=f"contract-toggle-owner-{uuid4().hex[:12]}",
                api_key=owner_key,
            )
            list_response: Response = await api_client.get(
                "/api/v1/auth/list", headers=owner_headers
            )
            assert list_response.status_code == 200
            api_keys: list[dict[str, object]] = list_response.json()["api_keys"]
            assert len(api_keys) == 1
            api_key_id = cast(str, api_keys[0]["id"])

        response: Response = await api_client.put(f"/api/v1/auth/{api_key_id}/toggle")

        if belongs_to_another_user:
            owner_response: Response = await api_client.get(
                f"/api/v1/auth/{api_key_id}", headers=owner_headers
            )
            assert owner_response.status_code == 200
            assert owner_response.json()["is_active"] is True

    assert response.status_code == 404
    assert response.headers["x-request-id"]
    assert response.json() == {
        "success": False,
        "error": {
            "code": "NOT_FOUND",
            "message": "APIKey not found",
            "request_id": response.headers["x-request-id"],
            "details": {"resource": "APIKey", "id": api_key_id},
        },
    }


@pytest.mark.asyncio
async def test_should_disable_and_then_reenable_an_api_key_via_the_toggle_route(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    create_payload: dict[str, object] = {
        "name": f"contract-toggle-{uuid4().hex[:8]}",
        "enabled_modules": ["jobs"],
    }

    async with developer_api_client_factory() as api_client:
        developer_authorization = api_client.headers["Authorization"]
        create_response = await api_client.post("/api/v1/auth/create", json=create_payload)
        assert create_response.status_code == 200
        create_response_json = cast(dict[str, object], create_response.json())
        raw_api_key = cast(str, create_response_json["api_key"])

        list_response = await api_client.get("/api/v1/auth/list")
        assert list_response.status_code == 200
        list_response_json = cast(dict[str, object], list_response.json())
        api_keys = cast(list[dict[str, object]], list_response_json["api_keys"])
        created_api_key = next(
            api_key
            for api_key in api_keys
            if api_key["name"] == create_payload["name"]
        )
        created_api_key_id = cast(str, created_api_key["id"])

        api_client.headers.update({"Authorization": f"Bearer {raw_api_key}"})
        pre_toggle_response = await api_client.get("/api/v1/jobs")

        api_client.headers.update({"Authorization": developer_authorization})
        disable_response = await api_client.put(
            f"/api/v1/auth/{created_api_key_id}/toggle"
        )

        api_client.headers.update({"Authorization": f"Bearer {raw_api_key}"})
        disabled_key_response = await api_client.get("/api/v1/jobs")

        api_client.headers.update({"Authorization": developer_authorization})
        enable_response = await api_client.put(
            f"/api/v1/auth/{created_api_key_id}/toggle"
        )

        api_client.headers.update({"Authorization": f"Bearer {raw_api_key}"})
        reenabled_key_response = await api_client.get("/api/v1/jobs")

    assert pre_toggle_response.status_code == 200
    assert disable_response.status_code == 200
    assert disable_response.json() == {"message": "API key status updated"}
    assert disabled_key_response.status_code == 401
    assert disabled_key_response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert enable_response.status_code == 200
    assert enable_response.json() == {"message": "API key status updated"}
    assert reenabled_key_response.status_code == 200
