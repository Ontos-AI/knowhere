from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import cast
from uuid import uuid4

import pytest
from httpx import AsyncClient


async def _create_waiting_file_job(
    api_client: AsyncClient,
    *,
    namespace: str = "contract-jobs-delete",
) -> dict[str, object]:
    payload: dict[str, str] = {
        "namespace": namespace,
        "source_type": "file",
        "file_name": "contract-delete.pdf",
        "data_id": f"contract-job-delete-{uuid4().hex[:12]}",
    }

    response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 200
    return cast(dict[str, object], response.json())


@pytest.mark.asyncio
async def test_should_delete_a_job_and_hide_it_from_the_job_apis(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        created_job = await _create_waiting_file_job(api_client)
        job_id = cast(str, created_job["job_id"])

        delete_response = await api_client.delete(f"/api/v1/jobs/{job_id}")
        get_response = await api_client.get(f"/api/v1/jobs/{job_id}")
        list_response = await api_client.get("/api/v1/jobs")

    assert delete_response.status_code == 200

    delete_json = cast(dict[str, object], delete_response.json())
    assert delete_json["job_id"] == job_id
    assert delete_json["deleted"] is True
    assert delete_json["document_id"] == created_job["document_id"]

    assert get_response.status_code == 404

    assert list_response.status_code == 200
    list_json = cast(dict[str, object], list_response.json())
    assert list_json["total"] == 0
    assert list_json["jobs"] == []


@pytest.mark.asyncio
async def test_should_keep_other_jobs_listed_after_deleting_one(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        deleted_job = await _create_waiting_file_job(api_client)
        surviving_job = await _create_waiting_file_job(api_client)

        await api_client.delete(f"/api/v1/jobs/{deleted_job['job_id']}")
        list_response = await api_client.get("/api/v1/jobs")

    assert list_response.status_code == 200

    list_json = cast(dict[str, object], list_response.json())
    jobs = cast(list[dict[str, object]], list_json["jobs"])

    assert list_json["total"] == 1
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == surviving_job["job_id"]


@pytest.mark.asyncio
async def test_should_return_not_found_when_deleting_an_already_deleted_job(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        created_job = await _create_waiting_file_job(api_client)
        job_id = cast(str, created_job["job_id"])

        first_response = await api_client.delete(f"/api/v1/jobs/{job_id}")
        second_response = await api_client.delete(f"/api/v1/jobs/{job_id}")

    assert first_response.status_code == 200
    assert second_response.status_code == 404

    response_json = cast(dict[str, object], second_response.json())
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_should_return_not_found_when_deleting_an_unknown_job(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    missing_job_id = f"job_missing_{uuid4().hex[:12]}"

    async with developer_api_client_factory() as api_client:
        response = await api_client.delete(f"/api/v1/jobs/{missing_job_id}")

    assert response.status_code == 404

    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])

    assert error["code"] == "NOT_FOUND"
    assert error["details"] == {
        "resource": "Job",
        "id": missing_job_id,
    }


@pytest.mark.asyncio
async def test_should_not_archive_the_document_when_archiving_is_disabled(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        created_job = await _create_waiting_file_job(api_client)
        job_id = cast(str, created_job["job_id"])

        response = await api_client.delete(
            f"/api/v1/jobs/{job_id}", params={"archive_document": "false"}
        )

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    assert response_json["deleted"] is True
    assert response_json["document_archived"] is False
