"""Snapshot bootstrap and resource capabilities stay pinned after final publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
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
        for path, content in {
            "manifest.json": b"{}",
            "chunks.json": b'{"chunks":[]}',
            "doc_nav.json": b'{"sections":[]}',
            "source.pdf": b"%PDF-source",
            "page_citation_assets/page-1.jpg": b"baseline image",
        }.items()
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
            raw_objects[f"{prefix}{item['path']}"] = b"resource"

    class Objects:
        def __init__(self) -> None:
            self.signed: list[str] = []
            self.reads: list[str] = []

        def download_fileobj(self, key: str, bucket: str | None = None) -> bytes:
            self.reads.append(key)
            return raw_objects[key]

        def exists(self, key: str, bucket: str | None = None) -> bool:
            return key in raw_objects

        def generate_presigned_url(self, key: str, **kwargs: object) -> str:
            self.signed.append(key)
            return f"https://storage.invalid/{key}?capability={len(self.signed)}"

    objects = Objects()
    monkeypatch.setattr(JobFileStorage, "storage_adapter", property(lambda _: objects))
    application = FastAPI()
    setup_exception_handlers(application)
    application.include_router(router, prefix="/v2/jobs")
    owner: list[str] = [scope.user_ref]

    async def current_user() -> CurrentUser:
        return CurrentUser(user_id=owner[0], user_tier="developer")

    async def database_dependency() -> AsyncGenerator[object, None]:
        async with contract_db_session() as database:
            yield database

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
            first = await client.get(
                f"{endpoint}/objects",
                params={"path": "page_citation_assets/page-1.jpg"},
            )
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
            for path in (
                "../final/images/page.jpg",
                "phase_b/blackboard.json",
                "images/unlisted.jpg",
            ):
                rejected = await client.get(
                    f"{endpoint}/objects", params={"path": path}
                )
                assert rejected.status_code == 404
            owner[0] = "another-owner"
            reads_before: int = len(objects.reads)
            denied = await client.get(f"{endpoint}/readable")
            assert denied.status_code == 404
            assert len(objects.reads) == reads_before
            owner[0] = scope.user_ref
            corrupt_key: str = (
                f"results/{scope.job_ref}/snapshots/baseline/snapshot-objects.json"
            )
            corrupted = json.loads(raw_objects[corrupt_key])
            corrupted["archive_checksum_sha256"] = "b" * 64
            raw_objects[corrupt_key] = json.dumps(corrupted).encode()
            corrupt = await client.get(f"{endpoint}/readable")
            assert corrupt.status_code == 409
            del raw_objects[corrupt_key]
            historical = await client.get(f"{endpoint}/readable")
            assert historical.status_code == 404

    asyncio.run(exercise())
    engine.dispose()
