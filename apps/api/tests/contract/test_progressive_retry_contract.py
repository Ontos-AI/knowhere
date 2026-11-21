"""Retired ingestion retry routes stay out of the public HTTP contract."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_excludes_retired_ingestion_retries_from_openapi(
    developer_api_client_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
) -> None:
    async with developer_api_client_factory() as client:
        retired = await client.post("/api/v2/jobs/job_retired/enrichment/retry")
        assert retired.status_code == 404, retired.text

        openapi = await client.get("/api/openapi.json")
        assert openapi.status_code == 200, openapi.text
        paths: dict[str, object] = openapi.json()["paths"]
        assert "/v2/jobs/{job_id}/enrichment/retry" not in paths
        assert "/v1/jobs/{job_id}/indexing/retry" not in paths
        assert "/v2/jobs/{job_id}/indexing/retry" not in paths
