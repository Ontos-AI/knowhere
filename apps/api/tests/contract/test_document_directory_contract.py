"""Authenticated lightweight initial directory reads."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import cast
from uuid import uuid4

import pytest
from httpx import AsyncClient

from tests.support.contract_database import ContractDatabase


async def _seed_directory(
    *, ready: bool, owner: str = "local-dev-user"
) -> dict[str, object]:
    suffix: str = uuid4().hex[:10]
    document_id: str = f"doc_{suffix}"
    job_id: str = f"job_{suffix}"
    revision_id: str = f"rev_{suffix}"
    snapshot: dict[str, object] = {
        "document_id": document_id,
        "job_id": job_id,
        "job_result_id": revision_id,
        "namespace": "directory-contract",
        "directory_version": 1 if ready else 0,
        "directory_committed_at": "2026-10-29T12:00:00+00:00" if ready else None,
        "sections": [
            {
                "section_id": "sec_parent",
                "parent_section_id": None,
                "section_path": "doc.pdf/Chapter",
                "section_title": "Chapter",
                "section_level": 1,
                "sort_order": 0,
                "page_nums": [1, 2],
            },
            {
                "section_id": "sec_child",
                "parent_section_id": "sec_parent",
                "section_path": "doc.pdf/Chapter/Child",
                "section_title": "Child",
                "section_level": 2,
                "sort_order": 1,
                "page_nums": [2],
            },
        ]
        if ready
        else [],
    }
    await ContractDatabase.insert_document(
        document_id=document_id, user_id=owner, namespace="directory-contract"
    )
    await ContractDatabase.insert_job(job_id=job_id, user_id=owner, status="done")
    await ContractDatabase.insert_job_result(
        job_result_id=revision_id,
        job_id=job_id,
        document_id=document_id,
        document_metadata={"initial_directory": snapshot} if ready else {},
    )
    await ContractDatabase.execute(
        "UPDATE documents SET current_job_result_id=:revision WHERE document_id=:document",
        {"revision": revision_id, "document": document_id},
    )
    return snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [True, False])
async def test_directory_read_returns_only_committed_initial_outline(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    ready: bool,
) -> None:
    async with developer_api_client_factory() as client:
        snapshot: dict[str, object] = await _seed_directory(ready=ready)
        for version in ("v1", "v2"):
            response = await client.get(
                f"/api/{version}/documents/{snapshot['document_id']}/structure",
                params={
                    "job_result_id": str(snapshot["job_result_id"]),
                    "namespace": "directory-contract",
                },
            )
            assert response.status_code == 200
            assert response.json() == snapshot
        assert (
            await ContractDatabase.fetch_all(
                "SELECT id FROM document_scope_enrichments WHERE job_result_id=:revision",
                {"revision": snapshot["job_result_id"]},
            )
            == []
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fence", ["foreign", "archived", "deleted", "revision", "namespace"]
)
async def test_directory_read_fences_access_and_current_revision(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    fence: str,
) -> None:
    async with developer_api_client_factory() as client:
        owner: str = (
            f"foreign-{uuid4().hex[:10]}" if fence == "foreign" else "local-dev-user"
        )
        if fence == "foreign":
            await ContractDatabase.insert_user(user_id=owner)
        snapshot: dict[str, object] = await _seed_directory(ready=True, owner=owner)
        document_id: str = cast(str, snapshot["document_id"])
        if fence == "archived":
            await ContractDatabase.execute(
                "UPDATE documents SET status='archived' WHERE document_id=:document",
                {"document": document_id},
            )
        elif fence == "deleted":
            await ContractDatabase.execute(
                "DELETE FROM documents WHERE document_id=:document",
                {"document": document_id},
            )
        params: dict[str, str] = {
            "job_result_id": "old-revision"
            if fence == "revision"
            else str(snapshot["job_result_id"]),
            "namespace": "old-namespace"
            if fence == "namespace"
            else "directory-contract",
        }
        response = await client.get(
            f"/api/v2/documents/{document_id}/structure", params=params
        )
        assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("enrichment_status", ["coarse", "complete"])
async def test_directory_read_freezes_historical_structure_once_without_fine(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    enrichment_status: str,
) -> None:
    import json

    async with developer_api_client_factory() as client:
        snapshot: dict[str, object] = await _seed_directory(ready=False)
        await ContractDatabase.execute(
            "UPDATE job_results SET document_metadata=CAST(:metadata AS JSON) WHERE id=:revision",
            {
                "revision": snapshot["job_result_id"],
                "metadata": json.dumps({"enrichment_status": enrichment_status}),
            },
        )
        await ContractDatabase.insert_document_section(
            section_id=f"sec_{uuid4().hex[:10]}",
            user_id="local-dev-user",
            namespace="directory-contract",
            document_id=str(snapshot["document_id"]),
            job_result_id=str(snapshot["job_result_id"]),
            section_path="doc.pdf/Chapter",
            section_title="Historical chapter",
            section_level=1,
        )
        first = await client.get(
            f"/api/v2/documents/{snapshot['document_id']}/structure"
        )
        assert first.status_code == 200
        directory = first.json()
        assert directory["directory_version"] == 1
        assert directory["directory_committed_at"] is not None
        assert directory["sections"][0]["section_title"] == "Historical chapter"
        await ContractDatabase.execute(
            "UPDATE document_sections SET section_title='Later Fine title' WHERE job_result_id=:revision",
            {"revision": snapshot["job_result_id"]},
        )
        second = await client.get(
            f"/api/v2/documents/{snapshot['document_id']}/structure"
        )
        assert second.json() == directory
        persisted = await ContractDatabase.fetch_one(
            "SELECT document_metadata FROM job_results WHERE id=:revision",
            {"revision": snapshot["job_result_id"]},
        )
        assert persisted is not None
        assert persisted["document_metadata"]["initial_directory"] == directory
