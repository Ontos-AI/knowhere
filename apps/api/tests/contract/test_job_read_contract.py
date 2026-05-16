import importlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import uuid4

import pytest
from httpx import AsyncClient

from tests.support.contract_database import ContractDatabase


async def _create_waiting_file_job(api_client: AsyncClient) -> dict[str, object]:
    payload: dict[str, str] = {
        "namespace": "contract-jobs",
        "source_type": "file",
        "file_name": "contract-read.pdf",
        "data_id": f"contract-job-read-{uuid4().hex[:12]}",
    }

    response = await api_client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 200
    response_json = cast(dict[str, object], response.json())
    return response_json


@pytest.mark.asyncio
async def test_should_list_created_jobs_for_the_authenticated_developer(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        created_job = await _create_waiting_file_job(api_client)

        response = await api_client.get("/api/v1/jobs")

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    jobs = cast(list[dict[str, object]], response_json["jobs"])

    assert response_json["total"] == 1
    assert response_json["page"] == 1
    assert response_json["page_size"] == 20
    assert response_json["total_pages"] == 1
    assert len(jobs) == 1

    job = jobs[0]
    assert job["job_id"] == created_job["job_id"]
    assert job["namespace"] == "contract-jobs"
    assert job["document_id"] is None
    assert job["status"] == "waiting-file"
    assert job["source_type"] == "file"
    assert job["data_id"] == created_job["data_id"]
    assert job["progress"] is None
    assert job["error"] is None
    assert job["result"] is None
    assert job["result_url"] is None
    assert job["created_at"]
    assert job["result_url_expires_at"]
    assert job["file_name"] == "contract-read.pdf"
    assert job["file_extension"] == "PDF"
    assert job["model"] is None
    assert job["ocr_enabled"] is None
    assert job["duration_seconds"] is not None
    assert job["credits_spent"] == 0.0


@pytest.mark.asyncio
async def test_should_list_jobs_when_date_filters_use_iso_utc_timezone(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    start_time = datetime.now(timezone.utc) - timedelta(days=1)
    end_time = datetime.now(timezone.utc) + timedelta(days=1)
    query_params = {
        "start_time": start_time.isoformat().replace("+00:00", "Z"),
        "end_time": end_time.isoformat().replace("+00:00", "Z"),
    }

    async with developer_api_client_factory() as api_client:
        created_job = await _create_waiting_file_job(api_client)

        response = await api_client.get("/api/v1/jobs/page", params=query_params)

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())
    jobs = cast(list[dict[str, object]], response_json["jobs"])

    assert response_json["total"] == 1
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == created_job["job_id"]


@pytest.mark.asyncio
async def test_should_return_job_details_for_an_existing_waiting_file_job(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as api_client:
        created_job = await _create_waiting_file_job(api_client)
        job_id = cast(str, created_job["job_id"])

        response = await api_client.get(f"/api/v1/jobs/{job_id}")

    assert response.status_code == 200

    response_json = cast(dict[str, object], response.json())

    assert response_json["job_id"] == job_id
    assert response_json["namespace"] == "contract-jobs"
    assert response_json["document_id"] is None
    assert response_json["status"] == "waiting-file"
    assert response_json["source_type"] == "file"
    assert response_json["data_id"] == created_job["data_id"]
    assert response_json["created_at"]
    assert response_json["progress"] is None
    assert response_json["error"] is None
    assert response_json["result"] is None
    assert response_json["result_url"] is None
    assert response_json["result_url_expires_at"]
    assert response_json["file_name"] == "contract-read.pdf"
    assert response_json["file_extension"] == "PDF"
    assert response_json["model"] is None
    assert response_json["ocr_enabled"] is None
    assert response_json["duration_seconds"] is not None
    assert response_json["credits_spent"] == 0.0


@pytest.mark.asyncio
async def test_should_return_not_found_when_requesting_an_unknown_job(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    missing_job_id = f"job_missing_{uuid4().hex[:12]}"

    async with developer_api_client_factory() as api_client:
        response = await api_client.get(f"/api/v1/jobs/{missing_job_id}")

    assert response.status_code == 404
    assert response.headers["x-request-id"]

    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "NOT_FOUND"
    assert error["message"] == "Job not found"
    assert error["details"] == {
        "resource": "Job",
        "id": missing_job_id,
    }


@pytest.mark.asyncio
async def test_should_forbid_access_to_a_job_owned_by_another_user(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    other_api_key = f"sk_contract_{uuid4().hex[:24]}"

    async with developer_api_client_factory() as api_client:
        await ContractDatabase.insert_authenticated_user(
            user_id=f"contract-user-{uuid4().hex[:12]}",
            api_key=other_api_key,
        )
        created_job = await _create_waiting_file_job(api_client)
        job_id = cast(str, created_job["job_id"])

        api_client.headers.update({"Authorization": f"Bearer {other_api_key}"})
        response = await api_client.get(f"/api/v1/jobs/{job_id}")

    assert response.status_code == 403
    assert response.headers["x-request-id"]

    response_json = cast(dict[str, object], response.json())
    error = cast(dict[str, object], response_json["error"])

    assert response_json["success"] is False
    assert error["code"] == "PERMISSION_DENIED"
    assert error["message"] == "You don't have permission to access this job"
    assert "details" not in error


def test_job_read_workflow_module_should_be_importable() -> None:
    job_read_service = importlib.import_module("app.services.jobs.read_service")

    assert callable(job_read_service.list_jobs_for_user)
    assert callable(job_read_service.get_job_result_for_user)
