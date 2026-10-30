"""Snapshot bootstrap and resource capabilities stay pinned after final publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shared.models.database.document import Document
from shared.models.database.job_result import JobResult
from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)
from tests.support.publication_benchmark_support import ensure_benchmark_import_path
from tests.support.retrieval_snapshot_support import contract_db_session

ensure_benchmark_import_path()
from scripts.publication_benchmark.publication_execution import (  # noqa: E402
    PublicationScope,
    seed_publication_fixture,
)


def test_readable_delivery_is_authorized_and_pinned_to_retained_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
) -> None:
    configure_contract_environment(monkeypatch, postgresql_proc)
    asyncio.run(prepare_contract_storage())
    from app.api.dependencies.current_user import with_current_user
    from app.api.v2.routes.jobs import router
    from app.core.exception_handlers import setup_exception_handlers
    from app.services.rate_limit.data_structures import CurrentUser
    from shared.core.database import get_db
    from shared.services.storage.job_file_storage import JobFileStorage

    scope = PublicationScope(
        user_ref="snapshot-owner",
        namespace_ref="snapshot-test",
        source_file_name="report.pdf",
        job_ref="snapshot-job",
        revision_ref="snapshot-result",
    )
    engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    snapshots: list[dict[str, str]] = [
        {
            "snapshot_id": stage,
            "stage": stage,
            "job_id": scope.job_ref,
            "job_result_id": scope.revision_ref,
            "document_id": "snapshot-document",
            "checksum": checksum,
            "archive_key": f"results/{scope.job_ref}/snapshots/{stage}.zip",
        }
        for stage, checksum in (("baseline", "a" * 64), ("final", "b" * 64))
    ]
    with Session(engine) as database:
        seed_publication_fixture(database, scope=scope)
        database.add(
            Document(
                document_id="snapshot-document",
                user_id=scope.user_ref,
                namespace=scope.namespace_ref,
                current_job_result_id=scope.revision_ref,
            )
        )
        database.flush()
        result: JobResult | None = database.get(JobResult, scope.revision_ref)
        assert result is not None
        result.document_id = "snapshot-document"
        result.document_metadata = {
            "progressive": {"snapshots": snapshots, "current_snapshot_id": "final"}
        }
        database.commit()

    readable_resources: dict[str, bytes] = {
        "manifest.json": b"{}",
        "chunks.json": b'{"chunks":[]}',
        "doc_nav.json": b'{"sections":[]}',
        "source.pdf": b"%PDF-source",
        **{
            f"page_citation_assets/page-{page}.jpg": f"page image {page}".encode()
            for page in range(1, 189)
        },
    }
    inventory_objects: list[dict[str, object]] = [
        {
            "path": path,
            "size_bytes": len(content),
            "checksum_sha256": hashlib.sha256(content).hexdigest(),
            "content_type": "image/jpeg"
            if path.endswith(".jpg")
            else "application/json",
            "kind": "image" if path.endswith(".jpg") else "essential",
        }
        for path, content in readable_resources.items()
    ]
    raw_objects: dict[str, bytes] = {}
    for snapshot in snapshots:
        prefix: str = snapshot["archive_key"].removesuffix(".zip") + "/"
        raw_objects[f"{prefix}snapshot-objects.json"] = json.dumps(
            {
                "version": "1.0",
                "archive_checksum_sha256": snapshot["checksum"],
                "objects": inventory_objects,
            }
        ).encode()
        for item in inventory_objects:
            raw_objects[f"{prefix}{item['path']}"] = readable_resources[str(item["path"])]

    class Objects:
        def __init__(self) -> None:
            self.signed: list[str] = []
            self.reads: list[str] = []
            self.heads: list[str] = []
            self.signature_count: int = 0

        def download_fileobj(self, key: str, bucket: str | None = None) -> bytes:
            self.reads.append(key)
            return raw_objects[key]

        def exists(self, key: str, bucket: str | None = None) -> bool:
            self.heads.append(key)
            return key in raw_objects

        def generate_presigned_url(self, key: str, **kwargs: object) -> str:
            self.signed.append(key)
            self.signature_count += 1
            return f"https://storage.invalid/{key}?capability={self.signature_count}"

    objects = Objects()
    monkeypatch.setattr(JobFileStorage, "storage_adapter", property(lambda _: objects))
    application = FastAPI()
    setup_exception_handlers(application)
    application.include_router(router, prefix="/v2/jobs")
    owner: list[str] = [scope.user_ref]
    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    def reset_operations() -> None:
        objects.reads.clear()
        objects.heads.clear()
        objects.signed.clear()
        statements.clear()

    async def current_user() -> CurrentUser:
        return CurrentUser(user_id=owner[0], user_tier="developer")

    async def database_dependency() -> AsyncGenerator[object, None]:
        async with contract_db_session() as database:
            bind = database.sync_session.get_bind()
            event.listen(bind, "before_cursor_execute", record_statement)
            try:
                yield database
            finally:
                event.remove(bind, "before_cursor_execute", record_statement)

    application.dependency_overrides[with_current_user] = current_user
    application.dependency_overrides[get_db] = database_dependency

    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            endpoint: str = f"/v2/jobs/{scope.job_ref}/snapshots/baseline"
            response = await client.get(f"{endpoint}/readable")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["snapshot_id"] == "baseline"
            assert payload["checksum"] == "a" * 64
            assert payload["objects"] == inventory_objects
            assert "archive_key" not in payload
            reset_operations()
            first = await client.get(
                f"{endpoint}/objects",
                params={"path": "page_citation_assets/page-1.jpg"},
            )
            assert objects.heads == [
                f"results/{scope.job_ref}/snapshots/baseline/snapshot-objects.json",
                f"results/{scope.job_ref}/snapshots/baseline/page_citation_assets/page-1.jpg",
            ], "single-object GET preserves its existing existence check"
            second = await client.get(
                f"{endpoint}/objects",
                params={"path": "page_citation_assets/page-1.jpg"},
            )
            assert first.status_code == second.status_code == 200
            assert (
                first.json()["snapshot_id"]
                == second.json()["snapshot_id"]
                == "baseline"
            )
            assert first.json()["download_url"] != second.json()["download_url"]
            assert all("/baseline/" in key for key in objects.signed)
            assert first.json()["expires_at"]
            image_paths: list[str] = [
                f"page_citation_assets/page-{page}.jpg" for page in range(1, 189)
            ]
            inventory_key: str = f"results/{scope.job_ref}/snapshots/baseline/snapshot-objects.json"
            reset_operations()
            batch = await client.post(
                f"{endpoint}/objects/access", json={"paths": image_paths}
            )
            assert batch.status_code == 200, batch.text
            assert len(statements) == 2, "one owner lookup and one snapshot lookup per batch"
            assert objects.heads == [inventory_key], "batch must not HEAD every image"
            assert objects.reads == [inventory_key], "batch must not download image bodies"
            assert len(objects.signed) == 188
            assert all("/baseline/" in key for key in objects.signed)
            batch_payload = batch.json()
            assert batch_payload["snapshot_id"] == "baseline"
            assert batch_payload["job_id"] == scope.job_ref
            assert batch_payload["job_result_id"] == scope.revision_ref
            assert batch_payload["document_id"] == "snapshot-document"
            assert batch_payload["checksum"] == "a" * 64
            assert [item["path"] for item in batch_payload["objects"]] == image_paths
            assert all(item["expires_at"] for item in batch_payload["objects"])
            assert "archive_key" not in batch_payload
            assert "stage" not in batch_payload
            final_batch = await client.post(
                f"/v2/jobs/{scope.job_ref}/snapshots/final/objects/access",
                json={"paths": image_paths[:2]},
            )
            assert final_batch.status_code == 200, final_batch.text
            assert final_batch.json()["checksum"] == "b" * 64
            assert final_batch.json()["job_result_id"] == batch_payload["job_result_id"]
            assert all("/final/" in item["download_url"] for item in final_batch.json()["objects"])
            refreshed = await client.post(
                f"{endpoint}/objects/access", json={"paths": image_paths[:1]}
            )
            assert refreshed.status_code == 200
            assert refreshed.json()["objects"][0]["download_url"] != batch_payload["objects"][0]["download_url"]

            for payload in (
                {},
                {"paths": []},
                {"paths": [image_paths[0], image_paths[0]]},
                {"paths": [""]},
                {"paths": ["x" * 2049]},
                {"paths": [None]},
                {"paths": image_paths + [f"images/extra-{index}.jpg" for index in range(13)]},
                {"paths": image_paths[:1], "prefix": "results/another-owner/"},
            ):
                reset_operations()
                invalid_batch = await client.post(f"{endpoint}/objects/access", json=payload)
                assert invalid_batch.status_code == 400, invalid_batch.text
                assert invalid_batch.json()["error"]["code"] == "INVALID_ARGUMENT"
                assert objects.reads == objects.heads == objects.signed == []
                assert statements == []

            for path in (
                "../final/images/page.jpg",
                "phase_b/blackboard.json",
                "images/unlisted.jpg",
            ):
                rejected = await client.get(
                    f"{endpoint}/objects", params={"path": path}
                )
                assert rejected.status_code == 404
                reset_operations()
                rejected_batch = await client.post(
                    f"{endpoint}/objects/access", json={"paths": [image_paths[0], path]}
                )
                assert rejected_batch.status_code == 404
                assert objects.signed == [], "validate the entire batch before signing any path"
                assert objects.heads == objects.reads == [inventory_key]
            owner[0] = "another-owner"
            reads_before: int = len(objects.reads)
            denied = await client.get(f"{endpoint}/readable")
            assert denied.status_code == 404
            assert len(objects.reads) == reads_before
            reset_operations()
            denied_batch = await client.post(
                f"{endpoint}/objects/access", json={"paths": image_paths}
            )
            assert denied_batch.status_code == 404
            assert len(statements) == 1
            assert objects.reads == objects.heads == objects.signed == []
            owner[0] = scope.user_ref

            for identity_field in ("job_id", "job_result_id", "document_id"):
                invalid_snapshots: list[dict[str, str]] = [
                    {**snapshots[0], identity_field: "another-identity"}, snapshots[1]
                ]
                with Session(engine) as database:
                    result = database.get(JobResult, scope.revision_ref)
                    assert result is not None
                    result.document_metadata = {
                        "progressive": {
                            "snapshots": invalid_snapshots,
                            "current_snapshot_id": "final",
                        }
                    }
                    database.commit()
                reset_operations()
                invalid_pin = await client.post(
                    f"{endpoint}/objects/access", json={"paths": image_paths}
                )
                assert invalid_pin.status_code == 404, invalid_pin.text
                assert invalid_pin.json()["error"]["details"]["resource"] == "Snapshot"
                assert len(statements) == 2
                assert objects.reads == objects.heads == objects.signed == []
            with Session(engine) as database:
                result = database.get(JobResult, scope.revision_ref)
                assert result is not None
                result.document_metadata = {
                    "progressive": {"snapshots": snapshots, "current_snapshot_id": "final"}
                }
                database.commit()

            corrupt_key: str = (
                f"results/{scope.job_ref}/snapshots/baseline/snapshot-objects.json"
            )
            corrupted = json.loads(raw_objects[corrupt_key])
            corrupted["archive_checksum_sha256"] = "b" * 64
            raw_objects[corrupt_key] = json.dumps(corrupted).encode()
            corrupt = await client.get(f"{endpoint}/readable")
            assert corrupt.status_code == 409
            reset_operations()
            corrupt_batch = await client.post(f"{endpoint}/objects/access", json={"paths": image_paths})
            assert corrupt_batch.status_code == 409
            assert objects.signed == []
            del raw_objects[corrupt_key]
            historical = await client.get(f"{endpoint}/readable")
            assert historical.status_code == 404
            historical_batch = await client.post(f"{endpoint}/objects/access", json={"paths": image_paths})
            assert historical_batch.status_code == 404
            assert historical_batch.json()["error"]["details"]["resource"] == "Readable snapshot"

    asyncio.run(exercise())
    engine.dispose()
