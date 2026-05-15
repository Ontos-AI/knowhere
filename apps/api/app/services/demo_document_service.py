"""Demo Source materialization workflow."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import blake2b
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.demo_source_catalog import DemoSourceCatalog, DemoSourceDefinition

from shared.core.exceptions.domain_exceptions import ValidationException
from shared.models.database.demo_materialization import DemoMaterialization
from shared.models.database.document import Document
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.services.retrieval.cache_service import invalidate_retrieval_cache_namespaces
from shared.services.retrieval.publication_service import RetrievalPublicationService
from shared.services.storage.result_storage import get_result_storage


@dataclass(frozen=True)
class MaterializedDemoSource:
    """User-owned copy of one canonical demo source."""

    demo_source_id: str
    document_id: str
    status: str
    title: str
    mime_type: str
    size_bytes: int
    chunk_count: int


class DemoDocumentService:
    """Serves canonical demo data and copies it into user namespaces."""

    def __init__(
        self,
        *,
        catalog: DemoSourceCatalog | None = None,
        publication_service: RetrievalPublicationService | None = None,
    ) -> None:
        self._catalog = catalog or DemoSourceCatalog()
        self._publication_service = publication_service or RetrievalPublicationService()

    def get_catalog(self) -> dict[str, Any]:
        return self._catalog.get_catalog()

    def list_chunks(
        self,
        *,
        demo_source_id: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any] | None:
        return self._catalog.list_chunks(
            demo_source_id=demo_source_id,
            page=page,
            page_size=page_size,
        )

    def get_chunk(
        self,
        *,
        demo_source_id: str,
        demo_chunk_id: str,
    ) -> dict[str, Any] | None:
        return self._catalog.get_chunk(
            demo_source_id=demo_source_id,
            demo_chunk_id=demo_chunk_id,
        )

    def get_original_file_path(self, *, demo_source_id: str) -> Path | None:
        return self._catalog.get_original_file_path(demo_source_id=demo_source_id)

    def get_asset_file_path(
        self,
        *,
        demo_source_id: str,
        asset_path: str,
    ) -> Path | None:
        return self._catalog.get_asset_file_path(
            demo_source_id=demo_source_id,
            asset_path=asset_path,
        )

    async def materialize_sources(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        demo_source_ids: list[str],
    ) -> list[MaterializedDemoSource]:
        selected_demo_source_ids = _deduplicate_source_ids(demo_source_ids)
        if not selected_demo_source_ids:
            raise ValidationException(
                user_message="At least one demo source must be selected.",
                violations=[
                    {
                        "field": "demo_source_ids",
                        "description": "Select one or more demo source IDs.",
                    }
                ],
            )

        selected_sources = [
            self._catalog.require_source(demo_source_id)
            for demo_source_id in selected_demo_source_ids
        ]
        results: list[MaterializedDemoSource] = []
        for source in selected_sources:
            result = await self._materialize_source(
                db,
                user_id=user_id,
                namespace=namespace,
                source=source,
            )
            results.append(result)

        await db.commit()
        await invalidate_retrieval_cache_namespaces(
            user_id=user_id,
            namespaces=[namespace],
        )
        return results

    async def _materialize_source(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        source: DemoSourceDefinition,
    ) -> MaterializedDemoSource:
        await _lock_materialization_scope(
            db,
            user_id=user_id,
            namespace=namespace,
            demo_source_id=source.demo_source_id,
        )
        existing = await self._get_existing_materialization(
            db,
            user_id=user_id,
            namespace=namespace,
            demo_source_id=source.demo_source_id,
        )
        if existing is not None and await self._is_active_document(
            db,
            document_id=existing.document_id,
        ):
            return _materialized_source_payload(
                source=source,
                document_id=existing.document_id,
                status="existing",
            )

        document_id = f"doc_{uuid4().hex[:12]}"
        job_id = f"job_demo_{uuid4().hex[:12]}"
        job_result_id = str(uuid4())
        timestamp = _utc_now()
        result_bundle = _upload_demo_result_bundle(
            job_id=job_id,
            source_directory=self._catalog.source_directory(source),
        )

        db.add(
            Job(
                job_id=job_id,
                user_id=user_id,
                job_type="demo_materialization",
                status="done",
                source_type="demo",
                webhook_enabled=False,
                job_metadata={
                    "document_id": document_id,
                    "namespace": namespace,
                    "source_type": "demo",
                    "source_file_name": source.title,
                    "demo_source_id": source.demo_source_id,
                },
                version=0,
                created_at=timestamp,
                updated_at=timestamp,
                credits_charged=0,
                billing_status="skipped",
            )
        )
        db.add(
            JobResult(
                id=job_result_id,
                job_id=job_id,
                delivery_mode="inline",
                document_metadata={
                    "source_file_name": source.title,
                    "demo_source_id": source.demo_source_id,
                },
                inline_payload={"source": "canonical_demo"},
                result_s3_key=result_bundle["zip_key"],
                result_size=result_bundle["zip_size"],
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        await db.flush()
        chunks = self._catalog.publication_chunks(source)
        await db.run_sync(
            lambda sync_db: self._publication_service.publish_document_state(
                sync_db,
                job_id=job_id,
                job_result_id=job_result_id,
                chunks=[dict(chunk) for chunk in chunks],
            )
        )
        await db.run_sync(
            lambda sync_db: self._publication_service.publish_document_graph(
                sync_db,
                job_id=job_id,
                job_result_id=job_result_id,
            )
        )
        await db.flush()

        if existing is None:
            db.add(
                DemoMaterialization(
                    user_id=user_id,
                    namespace=namespace,
                    demo_source_id=source.demo_source_id,
                    document_id=document_id,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        else:
            existing.document_id = document_id
            existing.updated_at = timestamp
        await db.flush()
        return _materialized_source_payload(
            source=source,
            document_id=document_id,
            status="created",
        )

    async def _get_existing_materialization(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        namespace: str,
        demo_source_id: str,
    ) -> DemoMaterialization | None:
        result = await db.execute(
            select(DemoMaterialization)
            .where(DemoMaterialization.user_id == user_id)
            .where(DemoMaterialization.namespace == namespace)
            .where(DemoMaterialization.demo_source_id == demo_source_id)
            .with_for_update()
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _is_active_document(
        self,
        db: AsyncSession,
        *,
        document_id: str,
    ) -> bool:
        result = await db.execute(
            select(Document.document_id)
            .where(Document.document_id == document_id)
            .where(Document.status == "active")
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


def _deduplicate_source_ids(demo_source_ids: list[str]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for demo_source_id in demo_source_ids:
        normalized = str(demo_source_id).strip()
        if not normalized or normalized in seen:
            continue
        selected.append(normalized)
        seen.add(normalized)
    return selected


async def _lock_materialization_scope(
    db: AsyncSession,
    *,
    user_id: str,
    namespace: str,
    demo_source_id: str,
) -> None:
    lock_id = _materialization_lock_id(
        user_id=user_id,
        namespace=namespace,
        demo_source_id=demo_source_id,
    )
    await db.execute(select(func.pg_advisory_xact_lock(lock_id)))


def _materialization_lock_id(
    *,
    user_id: str,
    namespace: str,
    demo_source_id: str,
) -> int:
    lock_key = f"{user_id}\0{namespace}\0{demo_source_id}"
    digest = blake2b(lock_key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _materialized_source_payload(
    *,
    source: DemoSourceDefinition,
    document_id: str,
    status: str,
) -> MaterializedDemoSource:
    return MaterializedDemoSource(
        demo_source_id=source.demo_source_id,
        document_id=document_id,
        status=status,
        title=source.title,
        mime_type=source.mime_type,
        size_bytes=source.size_bytes,
        chunk_count=source.chunk_count,
    )


def _upload_demo_result_bundle(
    *,
    job_id: str,
    source_directory: Path,
) -> dict[str, int | str]:
    with tempfile.TemporaryDirectory(prefix="knowhere-demo-result-") as temp_directory:
        zip_base_path = Path(temp_directory) / job_id
        zip_file_path = Path(
            shutil.make_archive(
                str(zip_base_path),
                "zip",
                root_dir=source_directory,
            )
        )
        zip_size = zip_file_path.stat().st_size
        bundle = get_result_storage().upload(
            job_id=job_id,
            result_dir=str(source_directory),
            zip_file_path=str(zip_file_path),
        )

    return {
        "zip_key": bundle.zip_key,
        "zip_size": zip_size,
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
