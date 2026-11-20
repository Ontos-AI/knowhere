from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from tests.support.contract_database import ContractDatabase


async def _document(
    document_id: str,
    *,
    namespace: str = "history",
    user_id: str = "local-dev-user",
    status: str = "active",
) -> None:
    await ContractDatabase.execute(
        """INSERT INTO documents (
            document_id, user_id, namespace, status, parse_track, created_at, updated_at
        ) VALUES (:id, :user, :namespace, :status, 'chunk', :timestamp, :timestamp)""",
        {
            "id": document_id,
            "user": user_id,
            "namespace": namespace,
            "status": status,
            "timestamp": datetime(2026, 1, 1),
        },
    )


@pytest.mark.parametrize("version", ["v1", "v2"])
async def test_document_history_keeps_attempts_and_revisions_after_archive(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    version: str,
) -> None:
    async with developer_api_client_factory() as client:
        await _document("doc_history")
        await _document("doc_other")
        await ContractDatabase.insert_user(user_id="other-history-user")
        timestamp = datetime(2026, 1, 1)
        for job_id, status, metadata, minute, owner in [
            ("job_old", "done", {}, 0, "local-dev-user"),
            (
                "job_failed",
                "failed",
                {"document_id": "doc_history"},
                1,
                "local-dev-user",
            ),
            ("job_current", "done", {"document_id": "doc_other"}, 2, "local-dev-user"),
            (
                "job_retry_a",
                "running",
                {"document_id": "doc_history"},
                3,
                "local-dev-user",
            ),
            (
                "job_retry_b",
                "failed",
                {"document_id": "doc_history"},
                3,
                "local-dev-user",
            ),
            (
                "job_conflict",
                "done",
                {"document_id": "doc_history"},
                4,
                "local-dev-user",
            ),
            (
                "job_foreign",
                "failed",
                {"document_id": "doc_history"},
                5,
                "other-history-user",
            ),
            ("job_unrelated", "done", {"namespace": "history"}, 6, "local-dev-user"),
        ]:
            await ContractDatabase.insert_job(
                job_id=job_id,
                user_id=owner,
                status=status,
                job_metadata=metadata,
                created_at=timestamp + timedelta(minutes=minute),
                error_code="INVALID_ARGUMENT" if status == "failed" else None,
            )
        for job_id, result_id, document_id in [
            ("job_old", "result_old", "doc_history"),
            ("job_current", "result_current", "doc_history"),
            ("job_retry_b", "result_legacy", None),
            ("job_conflict", "result_conflict", "doc_other"),
            ("job_foreign", "result_foreign", "doc_history"),
        ]:
            await ContractDatabase.insert_job_result(
                job_result_id=result_id,
                job_id=job_id,
                document_id=document_id,
            )
        for revision in ("old", "current"):
            await ContractDatabase.insert_document_chunk(
                chunk_id=f"chunk_{revision}",
                user_id="local-dev-user",
                namespace="history",
                document_id="doc_history",
                job_result_id=f"result_{revision}",
                section_id=None,
                chunk_type="text",
                content=f"{revision} revision content",
                section_path=f"{revision}.json",
            )
        await ContractDatabase.execute(
            "UPDATE documents SET current_job_result_id = 'result_current' WHERE document_id = 'doc_history'"
        )
        url = f"/api/{version}/documents/doc_history/jobs"
        before = await client.get(url)
        assert before.status_code == 200
        body = before.json()
        assert body["document_id"] == "doc_history"
        assert body["namespace"] == "history"
        assert body["pagination"]["total"] == 5
        assert [job["job_id"] for job in body["jobs"]] == [
            "job_retry_a",
            "job_retry_b",
            "job_current",
            "job_failed",
            "job_old",
        ]
        assert [
            job["job_id"] for job in body["jobs"] if job["is_current_revision"]
        ] == ["job_current"]
        assert body["jobs"][0]["job_result_id"] is None
        assert body["jobs"][1]["job_result_id"] == "result_legacy"
        assert body["jobs"][1]["error_code"] == "INVALID_ARGUMENT"
        assert all("job_metadata" not in job for job in body["jobs"])
        older_result_id = next(
            job["job_result_id"] for job in body["jobs"] if job["job_id"] == "job_old"
        )
        chunks_url = f"/api/{version}/documents/doc_history/chunks"
        older_revision = await client.get(
            chunks_url, params={"job_result_id": older_result_id}
        )
        assert older_revision.status_code == 200
        older_payload = older_revision.json()
        assert older_payload["job_result_id"] == "result_old"
        assert older_payload["job_id"] == "job_old"
        assert [chunk["content"] for chunk in older_payload["chunks"]] == [
            "old revision content"
        ]
        pages = [
            await client.get(url, params={"page": page, "page_size": 2})
            for page in (1, 2, 3, 4)
        ]
        assert [
            job["job_id"] for response in pages for job in response.json()["jobs"]
        ] == [job["job_id"] for job in body["jobs"]]
        assert pages[-1].json()["pagination"] == {
            "page": 4,
            "page_size": 2,
            "total": 5,
            "total_pages": 3,
        }
        assert (
            await client.post(f"/api/{version}/documents/doc_history/archive")
        ).status_code == 200
        after = await client.get(url)
        assert after.status_code == 200
        assert after.json() == body
        archived_revision = await client.get(
            chunks_url, params={"job_result_id": older_result_id}
        )
        assert archived_revision.status_code == 200
        assert archived_revision.json() == older_payload
        listing = await client.get(
            f"/api/{version}/documents", params={"namespace": "history"}
        )
        assert "doc_history" not in [
            document["document_id"] for document in listing.json()["documents"]
        ]
        assert (await client.get(f"/api/{version}/jobs/job_failed")).status_code == 200
        retained_job = await ContractDatabase.fetch_job("job_failed")
        assert retained_job is not None and retained_job["status"] == "failed"


@pytest.mark.parametrize("version", ["v1", "v2"])
async def test_document_history_is_empty_or_not_found_without_leaking_other_owners(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    version: str,
) -> None:
    async with developer_api_client_factory() as client:
        await _document("doc_empty", status="archived")
        await ContractDatabase.insert_user(user_id="history-foreign-owner")
        await _document("doc_foreign", user_id="history-foreign-owner")
        await ContractDatabase.insert_job(
            job_id="job_pending_upload",
            user_id="local-dev-user",
            job_metadata={"document_id": "doc_unmaterialized"},
        )
        response = await client.get(f"/api/{version}/documents/doc_empty/jobs")
        assert response.status_code == 200
        assert response.json()["jobs"] == []
        assert response.json()["pagination"]["total"] == 0
        for document_id in (
            "doc_missing",
            "doc_foreign",
            "doc_unmaterialized",
            "ddoc_missing",
        ):
            response = await client.get(f"/api/{version}/documents/{document_id}/jobs")
            assert response.status_code == 404


@pytest.mark.parametrize("version", ["v1", "v2"])
async def test_all_namespace_document_listing_preserves_defaults_and_owner_scope(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
    version: str,
) -> None:
    async with developer_api_client_factory() as client:
        from shared.core.config import settings
        from shared.core.database import get_db_context
        from shared.models.database.demo_corpus import DemoDocument
        from shared.services.retrieval.demo_authorization import (
            authorize_demo_transaction,
        )

        settings.DEMO_MAINTAINER_USER_IDS = "local-dev-user"
        async with get_db_context() as db:
            await db.run_sync(
                lambda session: authorize_demo_transaction(
                    session, user_id="local-dev-user"
                )
            )
            db.add(
                DemoDocument(
                    document_id="ddoc_shared_history",
                    demo_source_id="history-demo",
                    title="Synthetic shared document",
                    user_id="__knowhere_demo__",
                    namespace="__knowhere_demo__",
                    status="active",
                )
            )
            await db.commit()
        settings.DEMO_MAINTAINER_USER_IDS = ""
        await ContractDatabase.insert_job(
            job_id="job_shared_publisher",
            user_id="local-dev-user",
            status="done",
            job_metadata={"document_id": "ddoc_shared_history"},
        )
        await _document("doc_a", namespace="alpha")
        await ContractDatabase.insert_job(
            job_id="job_demo_conflict",
            user_id="local-dev-user",
            status="done",
            job_metadata={"document_id": "doc_a"},
        )
        await ContractDatabase.insert_job_result(
            job_result_id="result_demo_conflict",
            job_id="job_demo_conflict",
        )
        await ContractDatabase.execute(
            "UPDATE job_results SET demo_document_id = 'ddoc_shared_history' WHERE id = 'result_demo_conflict'"
        )
        await _document("doc_b", namespace="beta")
        await _document("doc_default", namespace="default")
        await _document("doc_archived", namespace="archived-only", status="archived")
        await ContractDatabase.insert_user(user_id="list-foreign-owner")
        await _document(
            "doc_foreign", namespace="foreign", user_id="list-foreign-owner"
        )
        url = f"/api/{version}/documents"
        for params in ({}, {"namespace": ""}, {"namespace": "  "}):
            response = await client.get(url, params=params)
            assert response.status_code == 200
            assert response.json()["namespace"] == "default"
            assert [
                document["document_id"] for document in response.json()["documents"]
            ] == ["doc_default"]
        pages = [
            await client.get(
                url, params={"all_namespaces": "true", "page": page, "page_size": 2}
            )
            for page in (1, 2)
        ]
        assert all(response.status_code == 200 for response in pages)
        assert pages[0].json()["namespace"] is None
        assert pages[0].json()["pagination"]["total"] == 3
        documents = [
            document for response in pages for document in response.json()["documents"]
        ]
        assert [document["document_id"] for document in documents] == [
            "doc_a",
            "doc_b",
            "doc_default",
        ]
        assert {document["namespace"] for document in documents} == {
            "alpha",
            "beta",
            "default",
        }
        demo_listing = await client.get(url, params={"namespace": "__knowhere_demo__"})
        assert demo_listing.status_code == 200
        assert "ddoc_shared_history" in [
            document["document_id"] for document in demo_listing.json()["documents"]
        ]
        demo_history = await client.get(f"{url}/ddoc_shared_history/jobs")
        assert demo_history.status_code == 200
        assert demo_history.json()["jobs"] == []
        assert demo_history.json()["pagination"]["total"] == 0
        private_history = await client.get(f"{url}/doc_a/jobs")
        assert private_history.status_code == 200
        assert private_history.json()["jobs"] == []
        assert private_history.json()["pagination"]["total"] == 0
        for namespace in ("alpha", ""):
            assert (
                await client.get(
                    url, params={"all_namespaces": "true", "namespace": namespace}
                )
            ).status_code == 400


async def test_document_history_validates_pagination_and_requires_authentication(
    developer_api_client_factory: Callable[
        [], AbstractAsyncContextManager[AsyncClient]
    ],
) -> None:
    async with developer_api_client_factory() as client:
        for version in ("v1", "v2"):
            url = f"/api/{version}/documents/doc_any/jobs"
            for params in ({"page": 0}, {"page_size": 0}, {"page_size": 201}):
                assert (await client.get(url, params=params)).status_code == 400
            response = await client.get(url, headers={"Authorization": ""})
            assert response.status_code == 401
