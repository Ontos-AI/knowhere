"""HTTP, PostgreSQL/RLS, and retrieval contracts for shared demo publication."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.support.contract_database import ContractDatabase

ClientFactory = Callable[[], AbstractAsyncContextManager[AsyncClient]]


async def create_demo(
    client: AsyncClient,
    *,
    source_id: str = "contract-demo",
    file_name: str = "shared.docx",
) -> dict[str, Any]:
    from shared.core.config import settings

    settings.DEMO_MAINTAINER_USER_IDS = "local-dev-user"
    response = await client.post(
        "/api/v2/jobs",
        json={
            "namespace": "__knowhere_demo__",
            "source_type": "file",
            "file_name": file_name,
            "data_id": source_id,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def publish_demo(
    job: dict[str, Any],
    *,
    content: str = "Shared launch capabilities include reusable rockets.",
    has_page: bool = False,
    should_fail: bool = False,
) -> str:
    from shared.core.database import get_db_context
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.services.retrieval.publication_service import (
        RetrievalPublicationService,
    )

    revisionId: str = str(uuid4())

    def publish(db: Session) -> None:
        record = db.execute(select(Job).where(Job.job_id == job["job_id"])).scalar_one()
        result = JobResult(
            id=revisionId,
            job_id=record.job_id,
            delivery_mode="url",
            document_metadata={
                "source_size_bytes": 123,
                "parse_track": "page_memory" if has_page else "chunk",
                "demo_asset_manifest": ["images/rocket.png", "tables/launch.html"],
            },
        )
        db.add(result)
        db.flush()
        chunks: list[dict[str, Any]] = [
            {
                "chunk_id": "body-" + revisionId[:8],
                "type": "page" if has_page else "text",
                "content": content,
                "path": "shared.docx/Capabilities",
                "metadata": {
                    "page_nums": [1] if has_page else [],
                    "connect_to": [
                        {"target": "image-" + revisionId[:8], "relation": "embeds"}
                    ],
                },
            },
            {
                "chunk_id": "image-" + revisionId[:8],
                "type": "image",
                "content": "Reusable rocket image",
                "path": "images/rocket.png",
                "metadata": {"file_path": "images/rocket.png"},
            },
            {
                "chunk_id": "table-" + revisionId[:8],
                "type": "table",
                "content": "<table><tr><td>Reusable launch</td></tr></table>",
                "path": "tables/launch.html",
                "metadata": {"file_path": "tables/launch.html"},
            },
        ]
        chunks.append(
            {
                "chunk_id": "filler-" + revisionId[:8],
                "type": "text",
                "content": "Independent astronomy paragraph about stars and planets.",
                "path": "shared.docx/Astronomy",
                "metadata": {},
            }
        )
        service = RetrievalPublicationService()
        outcome = service.publish_document_state(
            db, job_id=record.job_id, job_result_id=revisionId, chunks=chunks
        )
        assert outcome is not None and outcome.document_id == job["document_id"]
        service.publish_document_graph(
            db, job_id=record.job_id, job_result_id=revisionId
        )
        if should_fail:
            raise RuntimeError("Interrupted publication contract")
        record.status = "done"

    async with get_db_context() as db:
        await db.run_sync(publish)
        await db.commit()
    return revisionId


@pytest.mark.asyncio
async def test_preparing_readiness_and_removed_materialization(
    developer_api_client_factory: ClientFactory,
) -> None:
    async with developer_api_client_factory() as client:
        catalog = await client.get("/api/v1/demo/catalog")
        assert catalog.status_code == 200 and catalog.json()["sources"] == []
        assert catalog.headers["Cache-Control"] == "no-store"
        assert all(
            source["status"] == "planned"
            for source in catalog.json()["official_library"]["sources"]
        )
        query = await client.post(
            "/api/v2/retrieval/query",
            json={
                "namespace": "__knowhere_demo__",
                "query": "launch",
                "use_agentic": False,
            },
        )
        assert (
            query.status_code == 503
            and query.json()["error"]["code"] == "DEMO_NOT_READY"
        )
        original = await client.get("/api/v1/demo/sources/demo-spacex-s1/original")
        assert (
            original.status_code == 409
            and original.json()["error"]["code"] == "DEMO_NOT_READY"
        )
        removed = await client.post(
            "/api/v1/demo/materializations",
            json={"demo_source_ids": ["demo-spacex-s1"]},
        )
        assert (
            removed.status_code == 410
            and removed.json()["error"]["code"] == "DEMO_MATERIALIZATION_REMOVED"
        )


@pytest.mark.asyncio
async def test_demo_writes_require_maintainers(
    developer_api_client_factory: ClientFactory,
) -> None:
    async with developer_api_client_factory() as client:
        from shared.core.config import settings

        settings.DEMO_MAINTAINER_USER_IDS = ""
        for version in ("v1", "v2"):
            response = await client.post(
                f"/api/{version}/jobs",
                json={
                    "namespace": " __knowhere_demo__ ",
                    "source_type": "file",
                    "file_name": "shared.docx",
                    "data_id": "forbidden",
                },
            )
            assert response.status_code == 403, response.text
        archive = await client.post("/api/v1/documents/ddoc_forbidden/archive")
        assert archive.status_code == 403, archive.text
        from shared.core.database import get_db_context

        async with get_db_context() as db:
            result = await db.execute(
                text("UPDATE demo_documents SET title='Unauthorized'")
            )
            assert result.rowcount == 0
            with pytest.raises(DBAPIError):
                await db.execute(
                    text(
                        "INSERT INTO demo_documents (document_id, demo_source_id, user_id, namespace, status, title, category) VALUES ('ddoc_forbidden', 'forbidden', '__knowhere_demo__', '__knowhere_demo__', 'active', 'Unauthorized', 'other')"
                    )
                )
            await db.rollback()


@pytest.mark.asyncio
async def test_shared_lifecycle_reads_retrieval_history_and_global_cache(
    developer_api_client_factory: ClientFactory,
) -> None:
    async with developer_api_client_factory() as client:
        job = await create_demo(client)
        conflict = await client.post(
            "/api/v2/jobs",
            json={
                "namespace": "__knowhere_demo__",
                "source_type": "file",
                "file_name": "shared.docx",
                "data_id": "contract-demo",
            },
        )
        assert conflict.status_code == 409
        revisionId = await publish_demo(job)
        catalog = (await client.get("/api/v1/demo/catalog")).json()
        source = next(
            source
            for source in catalog["sources"]
            if source["demo_source_id"] == "contract-demo"
        )
        assert source["canonical_document_id"] == job["document_id"]
        assert (
            source["job_result_id"] == revisionId
            and source["chunk_count"] == 4
            and source["size_bytes"] == 123
        )
        readerKey: str = "sk_contract_reader_two"
        await ContractDatabase.insert_authenticated_user(
            user_id="reader-two",
            api_key=readerKey,
            credits_balance=10000000,
            user_tier="tier_5",
        )
        identities = [client.headers["Authorization"], "Bearer " + readerKey]
        queryPayload = {
            "namespace": "__knowhere_demo__",
            "query": "capabilities",
            "include_document_ids": [job["document_id"]],
            "use_agentic": False,
            "top_k": 1,
        }
        for identity in identities:
            response = await client.post(
                "/api/v2/retrieval/query",
                json=queryPayload,
                headers={"Authorization": identity},
            )
            assert response.status_code == 200, response.text
            assert (
                response.json()["results"]
                and response.json()["results"][0]["source"]["job_result_id"]
                == revisionId
            )
            document = await client.get(
                f"/api/v2/documents/{job['document_id']}",
                headers={"Authorization": identity},
            )
            assert (
                document.status_code == 200
                and document.json()["job_result_id"] == revisionId
            )
        chunks = (
            await client.get(f"/api/v2/documents/{job['document_id']}/chunks")
        ).json()
        chunkId = chunks["chunks"][0]["id"]
        update = await create_demo(client)
        assert update["document_id"] == job["document_id"]
        newRevision = await publish_demo(
            update, content="Updated capabilities include orbital refueling."
        )
        for identity in identities:
            response = await client.post(
                "/api/v2/retrieval/query",
                json={**queryPayload, "query": "capabilities"},
                headers={"Authorization": identity},
            )
            assert response.status_code == 200, response.text
            assert (
                response.json()["results"][0]["source"]["job_result_id"] == newRevision
            )
        history = await client.get(
            f"/api/v1/documents/{job['document_id']}/chunks/{chunkId}",
            params={"job_result_id": revisionId},
        )
        assert (
            history.status_code == 200
            and "reusable rockets" in history.json()["chunk"]["content"]
        )
        wrongRevision = await client.get(
            f"/api/v1/documents/{job['document_id']}/chunks",
            params={"job_result_id": str(uuid4())},
        )
        assert wrongRevision.status_code == 404
        failedUpdate = await create_demo(client)
        with pytest.raises(RuntimeError, match="Interrupted publication"):
            await publish_demo(failedUpdate, should_fail=True)
        document = await client.get(f"/api/v1/documents/{job['document_id']}")
        assert document.json()["job_result_id"] == newRevision
        from shared.core.config import settings

        settings.DEMO_MAINTAINER_USER_IDS = ""
        assert (
            await client.post(f"/api/v2/documents/{job['document_id']}/archive")
        ).status_code == 403
        settings.DEMO_MAINTAINER_USER_IDS = "local-dev-user"
        assert (
            await client.post(f"/api/v2/documents/{job['document_id']}/archive")
        ).status_code == 200
        assert (
            await client.get(
                f"/api/v1/documents/{job['document_id']}/chunks",
                params={"job_result_id": revisionId},
            )
        ).status_code == 404


@pytest.mark.asyncio
async def test_demo_agent_tools_pin_history_and_private_isolation(
    developer_api_client_factory: ClientFactory,
) -> None:
    async with developer_api_client_factory() as client:
        job = await create_demo(client, source_id="agent-demo")
        revision = await publish_demo(job, has_page=True)
        historyChunks = (
            await client.get(f"/api/v1/documents/{job['document_id']}/chunks")
        ).json()["chunks"]
        sectionPath = next(
            chunk["section_path"]
            for chunk in historyChunks
            if chunk["chunk_type"] == "page"
        )
        from shared.core.database import get_db_context
        from shared.services.retrieval.execution.revision_pins import (
            capture_revision_pins,
        )
        from shared.services.retrieval.corpus_revision_context import (
            CorpusRevisionContext,
        )
        from shared.services.retrieval.agent_explore.dispatch import dispatch_tool_call

        async with get_db_context() as db:
            pins = await capture_revision_pins(
                db, user_id="reader-two", namespace="__knowhere_demo__"
            )
        update = await create_demo(client, source_id="agent-demo")
        await publish_demo(
            update, content="Updated orbital refueling evidence", has_page=True
        )
        with CorpusRevisionContext.bind(pins):
            calls = [
                ("corpus.list_documents", {}),
                ("corpus.outline", {"scope": [{"document_id": job["document_id"]}]}),
                (
                    "corpus.grep",
                    {
                        "pattern": "reusable rockets",
                        "scope": [{"document_id": job["document_id"]}],
                    },
                ),
                (
                    "corpus.recall",
                    {
                        "query": "rockets",
                        "scope": [{"document_id": job["document_id"]}],
                    },
                ),
                ("corpus.assets", {"scope": [{"document_id": job["document_id"]}]}),
                ("corpus.neighbors", {"document_id": job["document_id"]}),
                (
                    "corpus.read",
                    {
                        "refs": [
                            {
                                "document_id": job["document_id"],
                                "chunk_id": "body-" + revision[:8],
                            }
                        ]
                    },
                ),
            ]
            for name, arguments in calls:
                result = await dispatch_tool_call(
                    name,
                    arguments,
                    db_factory=get_db_context,
                    user_id="reader-two",
                    namespace="__knowhere_demo__",
                )
                assert result.error is None, (name, result.error)
                assert "Updated orbital" not in result.text, (name, result.text)
                if name == "corpus.recall":
                    assert any(
                        ref.get("chunk_id") == "body-" + revision[:8]
                        for ref in result.refs
                    ), result.text
                if name == "corpus.read":
                    assert "reusable rockets" in result.text
                if name == "corpus.grep":
                    assert result.payload.get("total_matches", 0) > 0 or result.refs
            by_path = await dispatch_tool_call(
                "corpus.read",
                {
                    "refs": [
                        {"document_id": job["document_id"], "section_path": sectionPath}
                    ]
                },
                db_factory=get_db_context,
                user_id="reader-two",
                namespace="__knowhere_demo__",
            )
            assert by_path.error is None, by_path.error
            assert "reusable rockets" in by_path.text
            assert "Updated orbital" not in by_path.text
            assert any(
                ref.get("chunk_id") == "body-" + revision[:8] for ref in by_path.refs
            ), by_path.refs
        private = await client.post(
            "/api/v2/jobs",
            json={
                "namespace": "personal",
                "source_type": "file",
                "file_name": "shared.docx",
                "data_id": "private-demo-copy",
            },
        )
        assert private.status_code == 200, private.text
        await publish_demo(private.json())
        readerKey = "sk_private_isolation_reader"
        await ContractDatabase.insert_authenticated_user(
            user_id="isolated-reader",
            api_key=readerKey,
            credits_balance=10000000,
            user_tier="tier_5",
        )
        ownDocument = await client.get(
            f"/api/v1/documents/{private.json()['document_id']}"
        )
        assert ownDocument.status_code == 200
        denied = await client.get(
            f"/api/v1/documents/{private.json()['document_id']}",
            headers={"Authorization": "Bearer " + readerKey},
        )
        assert denied.status_code == 404
        shared = await client.post(
            "/api/v2/retrieval/query",
            json={
                "namespace": "__knowhere_demo__",
                "query": "orbital",
                "top_k": 100,
                "use_agentic": False,
            },
            headers={"Authorization": "Bearer " + readerKey},
        )
        assert shared.status_code == 200 and shared.json()["results"]
        assert all(
            row["source"]["document_id"] != private.json()["document_id"]
            for row in shared.json()["results"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_name", "original_mime"),
    [
        ("original.pdf", "application/pdf"),
        (
            "original.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            "original.DOCX",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    ],
)
async def test_demo_revision_media_validation_and_job_retention(
    developer_api_client_factory: ClientFactory,
    monkeypatch: pytest.MonkeyPatch,
    file_name: str,
    original_mime: str,
) -> None:
    async with developer_api_client_factory() as client:
        job = await create_demo(
            client, source_id="media-demo", file_name=file_name
        )
        revision = await publish_demo(job, has_page=True)
        import mimetypes
        from pathlib import PurePath
        from shared.services.storage.job_file_storage import JobFileStorage

        # Slim runtime images need not include the operating system MIME registry.
        mimetypes.init()
        monkeypatch.delitem(mimetypes.types_map, PurePath(file_name).suffix.lower(), raising=False)
        catalog = (await client.get("/api/v1/demo/catalog")).json()
        source = next(item for item in catalog["sources"] if item["demo_source_id"] == "media-demo")
        assert source["mime_type"] == original_mime
        assert source["original_file"]["mime_type"] == original_mime

        calls: list[dict[str, Any]] = []

        def sign(key: str, **arguments: Any) -> str:
            calls.append({"key": key, **arguments})
            return "https://storage.example.test/signed-object"

        class SignedStorage:
            generate_presigned_url = staticmethod(sign)

        monkeypatch.setattr(
            JobFileStorage, "storage_adapter", property(lambda self: SignedStorage())
        )
        for path, mime in (
            ("original", original_mime),
            ("assets/images/rocket.png", "image/png"),
            ("assets/tables/launch.html", "text/html"),
        ):
            response = await client.get(
                f"/api/v1/demo/sources/media-demo/{path}",
                params={"job_result_id": revision},
                headers={"Range": "bytes=0-10"},
                follow_redirects=False,
            )
            assert response.status_code == 307, response.text
            assert calls[-1]["headers"]["Content-Type"] == mime
            assert calls[-1]["headers"]["Content-Disposition"].startswith("inline;")
        for path in (
            "images/not-listed.png",
            "images/%2e%2e/secret.png",
            "%2fimages/rocket.png",
            "images/rocket.png/extra",
        ):
            response = await client.get(
                f"/api/v1/demo/sources/media-demo/assets/{path}",
                params={"job_result_id": revision},
            )
            assert response.status_code == 404, response.text
        chunks = await client.get(
            "/api/v1/demo/sources/media-demo/chunks",
            params={"page_size": 1, "job_result_id": revision},
        )
        assert (
            chunks.status_code == 200
            and len(chunks.json()["chunks"]) == 1
            and chunks.json()["pagination"]["total"] == 4
        )
        chunk = await client.get(
            f"/api/v1/demo/sources/media-demo/chunks/{chunks.json()['chunks'][0]['id']}",
            params={"job_result_id": revision},
        )
        assert chunk.status_code == 200 and chunk.json()["job_result_id"] == revision
        for version in ("v1", "v2"):
            deletion = await client.delete(f"/api/{version}/jobs/{job['job_id']}")
            assert (
                deletion.status_code == 409
                and "retained shared demo revision" in deletion.text
            )
        from shared.core.database import get_db_context

        async with get_db_context() as db:
            with pytest.raises(DBAPIError):
                await db.execute(
                    text("DELETE FROM jobs WHERE job_id=:identity"),
                    {"identity": job["job_id"]},
                )
            await db.rollback()


@pytest.mark.asyncio
async def test_demo_source_claims_worker_validation_and_interrupted_batch_roll_back(
    developer_api_client_factory: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with developer_api_client_factory() as client:
        import asyncio

        # Authorize before concurrent HTTP calls so the same source lock is
        # exercised by distinct real database transactions.
        from shared.core.config import settings

        settings.DEMO_MAINTAINER_USER_IDS = "local-dev-user"
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/v2/jobs",
                    json={
                        "namespace": "__knowhere_demo__",
                        "source_type": "file",
                        "file_name": "shared.docx",
                        "data_id": "concurrent-demo",
                    },
                )
                for _ in range(2)
            )
        )
        assert sorted(response.status_code for response in responses) == [200, 409]
        job = next(
            response.json() for response in responses if response.status_code == 200
        )
        renewed = await client.post(f"/api/v2/jobs/{job['job_id']}/upload-url")
        assert renewed.status_code == 200 and renewed.json()["upload_url"]
        settings.DEMO_MAINTAINER_USER_IDS = ""
        for suffix in ("upload-url", "confirm-upload"):
            denied = await client.post(
                f"/api/v2/jobs/{job['job_id']}/{suffix}", json={}
            )
            assert denied.status_code == 403, denied.text
        settings.DEMO_MAINTAINER_USER_IDS = "local-dev-user"
        from shared.core.database import get_db_context
        from shared.models.database.job import Job
        from shared.models.database.job_result import JobResult
        from shared.models.database.demo_corpus import DemoDocument, DemoDocumentChunk
        from shared.services.retrieval.demo_job_scope import validate_job_corpus
        from shared.services.retrieval.publication_service import (
            RetrievalPublicationService,
        )
        from sqlalchemy import event, func

        insertedBatches: list[int] = []

        def interrupt_chunk_batch(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            if statement.startswith("INSERT INTO demo_document_chunks"):
                insertedBatches.append(1)
                if len(insertedBatches) == 2:
                    raise RuntimeError("Second chunk batch interrupted")

        async with get_db_context() as db:

            def publish(session: Session) -> None:
                record = session.execute(
                    select(Job).where(Job.job_id == job["job_id"])
                ).scalar_one()
                malformed = dict(record.job_metadata or {})
                record.job_metadata = {**malformed, "corpus_target": "PRIVATE"}
                from shared.core.exceptions.domain_exceptions import (
                    PermissionDeniedException,
                )

                with pytest.raises(PermissionDeniedException):
                    validate_job_corpus(session, job=record)
                record.job_metadata = malformed
                session.add(
                    JobResult(
                        id=str(uuid4()), job_id=job["job_id"], delivery_mode="url"
                    )
                )
                session.flush()
                revision = session.execute(
                    select(JobResult.id).where(JobResult.job_id == job["job_id"])
                ).scalar_one()
                chunks: list[dict[str, Any]] = [
                    {
                        "chunk_id": f"batch-{index}",
                        "type": "text",
                        "content": f"Unique publication evidence {index}",
                        "path": f"shared.docx/Section {index}",
                        "metadata": {},
                    }
                    for index in range(1001)
                ]
                connection = session.connection()
                event.listen(connection, "before_cursor_execute", interrupt_chunk_batch)
                try:
                    RetrievalPublicationService().publish_document_state(
                        session,
                        job_id=job["job_id"],
                        job_result_id=revision,
                        chunks=chunks,
                    )
                finally:
                    event.remove(
                        connection, "before_cursor_execute", interrupt_chunk_batch
                    )

            with pytest.raises(RuntimeError, match="Second chunk batch"):
                await db.run_sync(publish)
            await db.rollback()
        assert len(insertedBatches) == 2
        async with get_db_context() as db:
            count = (
                await db.execute(
                    select(func.count())
                    .select_from(DemoDocumentChunk)
                    .where(DemoDocumentChunk.document_id == job["document_id"])
                )
            ).scalar_one()
            document = (
                await db.execute(
                    select(DemoDocument).where(
                        DemoDocument.document_id == job["document_id"]
                    )
                )
            ).scalar_one()
            assert count == 0 and document.current_job_result_id is None
        missing = await client.post(
            "/api/v2/retrieval/query",
            json={
                "namespace": "__knowhere_demo__",
                "query": "launch",
                "include_document_ids": [job["document_id"]],
                "use_agentic": False,
            },
        )
        assert missing.status_code == 409


@pytest.mark.asyncio
async def test_demo_filters_unpublished_revisions_examples_and_caller_stats(
    developer_api_client_factory: ClientFactory,
) -> None:
    async with developer_api_client_factory() as client:
        job = await create_demo(client, source_id="filtered-demo")
        from shared.core.database import get_db_context
        from shared.models.database.demo_corpus import DemoDocument
        from shared.models.database.job import Job
        from shared.models.database.job_result import JobResult
        from shared.services.retrieval.demo_authorization import (
            authorize_demo_transaction,
            authorize_demo_caller,
        )
        from shared.services.retrieval.stats.service import record_retrieval_hits

        async with get_db_context() as db:
            await db.run_sync(
                lambda session: authorize_demo_transaction(
                    session, user_id="local-dev-user"
                )
            )
            document = (
                await db.execute(
                    select(DemoDocument).where(
                        DemoDocument.document_id == job["document_id"]
                    )
                )
            ).scalar_one()
            document.catalog_metadata = {
                "examples": [
                    {
                        "id": "unique",
                        "question": "Which capability?",
                        "answer": "Reusable rockets.",
                        "citations": [
                            {
                                "content": "Shared launch capabilities include reusable rockets."
                            }
                        ],
                    },
                    {
                        "id": "missing",
                        "question": "Missing?",
                        "citations": [{"content": "Missing curated evidence"}],
                    },
                    {
                        "id": "ambiguous",
                        "question": "Reusable?",
                        "citations": [{"content": "Reusable"}],
                    },
                ]
            }
            await db.commit()
        revision = await publish_demo(job)
        catalog = (await client.get("/api/v1/demo/catalog")).json()
        source = next(
            row
            for row in catalog["sources"]
            if row["demo_source_id"] == "filtered-demo"
        )
        assert [example["id"] for example in source["examples"]] == ["unique"]
        assert source["examples"][0]["citations"][0]["job_result_id"] == revision
        chunks = (
            await client.get(f"/api/v2/documents/{job['document_id']}/chunks")
        ).json()["chunks"]
        section = next(
            row["section_path"]
            for row in chunks
            if row["chunk_type"] == "text" and "capabilities" in row["content"]
        )
        payload = {
            "namespace": "__knowhere_demo__",
            "query": "capabilities",
            "use_agentic": False,
            "top_k": 1,
            "include_document_ids": [job["document_id"]],
        }
        for filters in (
            {"include_document_ids": []},
            {"exclude_document_ids": [job["document_id"]]},
            {
                "exclude_sections": [
                    {"document_id": job["document_id"], "section_path": section}
                ]
            },
            {"signal_paths": ["unmatched-section"], "filter_mode": "keep"},
            {"query": "nonexistentuniquephrase"},
        ):
            result = await client.post(
                "/api/v2/retrieval/query", json={**payload, **filters}
            )
            assert result.status_code == 200 and result.json()["results"] == [], (
                result.text
            )
        for chunk_type in ("text", "image", "table"):
            result = await client.get(
                f"/api/v1/documents/{job['document_id']}/chunks",
                params={"chunk_type": chunk_type},
            )
            assert result.status_code == 200
            assert result.json()["chunks"] and all(
                row["chunk_type"] == chunk_type for row in result.json()["chunks"]
            )
        pending = await create_demo(client, source_id="unpublished-demo")
        staged_revision = str(uuid4())
        async with get_db_context() as db:
            record = (
                await db.execute(select(Job).where(Job.job_id == pending["job_id"]))
            ).scalar_one()
            record.status = "failed"
            db.add(
                JobResult(
                    id=staged_revision,
                    job_id=record.job_id,
                    demo_document_id=pending["document_id"],
                    delivery_mode="url",
                )
            )
            await db.commit()
        assert (
            await client.get(
                f"/api/v2/documents/{pending['document_id']}/chunks",
                params={"job_result_id": staged_revision},
            )
        ).status_code == 404
        query = await client.post(
            "/api/v2/retrieval/query",
            json={**payload, "include_document_ids": [pending["document_id"]]},
        )
        assert (
            query.status_code == 409
            and query.json()["error"]["code"] == "DEMO_NOT_READY"
        )
        wrong_source = await client.get(
            f"/api/v2/documents/{job['document_id']}/chunks",
            params={"job_result_id": staged_revision},
        )
        assert wrong_source.status_code == 404
        retry = await create_demo(client, source_id="unpublished-demo")
        assert retry["document_id"] == pending["document_id"]
        async with get_db_context() as db:
            await record_retrieval_hits(
                db,
                user_id="actual-reader",
                namespace="__knowhere_demo__",
                results=[
                    {
                        "document_id": job["document_id"],
                        "chunk_id": "body-" + revision[:8],
                    }
                ],
            )
            await db.commit()
        async with get_db_context() as db:
            assert not (
                await db.execute(text("SELECT user_id FROM demo_retrieval_hit_stats"))
            ).all()
            await db.run_sync(
                lambda session: authorize_demo_caller(session, user_id="actual-reader")
            )
            assert {
                row[0]
                for row in (
                    await db.execute(
                        text("SELECT user_id FROM demo_retrieval_hit_stats")
                    )
                ).all()
            } == {"actual-reader"}
            with pytest.raises(DBAPIError):
                await db.execute(
                    text(
                        "INSERT INTO demo_retrieval_hit_stats (id, user_id, namespace, hit_kind, document_id, hit_count, created_at, updated_at) VALUES ('forged-hit', 'other-reader', '__knowhere_demo__', 'document', :document, 1, now(), now())"
                    ),
                    {"document": job["document_id"]},
                )
            await db.rollback()
