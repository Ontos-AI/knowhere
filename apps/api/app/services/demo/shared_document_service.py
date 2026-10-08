"""Directory and media reads for the single shared, revisioned demo corpus."""

from __future__ import annotations

import asyncio
from pathlib import PurePath
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.services.demo.official_library_catalog import get_official_library_catalog
from app.services.demo.revision_reader import resolve_demo_revision
from shared.core.exceptions.domain_exceptions import NotFoundException
from shared.models.database.demo_corpus import (
    DemoDocument,
    DemoDocumentChunk,
    DemoDocumentMapUnitIndex,
)
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.services.retrieval.corpus_storage import DEMO_NAMESPACE
from shared.services.storage.job_file_storage import JobFileStorage
from shared.services.storage.demo_asset_signer import DemoAssetSigner


class SharedDemoDocumentService:
    async def get_catalog(self, db: AsyncSession) -> dict[str, Any]:
        sourceExpression = Job.job_metadata["demo_source_id"].as_string()
        latestJobs = (
            select(sourceExpression.label("source_id"), Job.status)
            .where(Job.job_metadata["corpus_target"].as_string() == "DEMO")
            .distinct(sourceExpression)
            .order_by(sourceExpression, Job.created_at.desc())
            .subquery()
        )
        counts = (
            select(
                DemoDocumentChunk.document_id,
                DemoDocumentChunk.job_result_id,
                func.count(DemoDocumentChunk.id).label("chunk_count"),
            )
            .group_by(DemoDocumentChunk.document_id, DemoDocumentChunk.job_result_id)
            .subquery()
        )
        catalogRows = (
            await db.execute(
                select(
                    DemoDocument,
                    JobResult,
                    latestJobs.c.status,
                    DemoDocumentMapUnitIndex.id,
                    counts.c.chunk_count,
                )
                .options(noload(JobResult.chunks), noload(JobResult.job))
                .outerjoin(
                    JobResult, JobResult.id == DemoDocument.current_job_result_id
                )
                .outerjoin(
                    latestJobs, latestJobs.c.source_id == DemoDocument.demo_source_id
                )
                .outerjoin(
                    DemoDocumentMapUnitIndex,
                    (DemoDocumentMapUnitIndex.document_id == DemoDocument.document_id)
                    & (
                        DemoDocumentMapUnitIndex.job_result_id
                        == DemoDocument.current_job_result_id
                    ),
                )
                .outerjoin(
                    counts,
                    (counts.c.document_id == DemoDocument.document_id)
                    & (counts.c.job_result_id == DemoDocument.current_job_result_id),
                )
                .where(DemoDocument.status == "active")
                .order_by(DemoDocument.title)
            )
        ).all()
        readySources: list[dict[str, Any]] = []
        importStatuses: list[dict[str, str]] = []
        for document, revision, latestStatus, marker, count in catalogRows:
            state: str = (
                "planned"
                if latestStatus is None
                else "processing"
                if latestStatus in ("waiting-file", "pending", "running", "converting")
                else "failed"
                if latestStatus == "failed"
                else "ready"
                if document.current_job_result_id
                else "planned"
            )
            importStatuses.append(
                {"demo_source_id": document.demo_source_id, "status": state}
            )
            if revision is None or marker is None:
                continue
            metadata: dict[str, Any] = revision.document_metadata or {}
            mimeType: str = (
                JobFileStorage.get_content_type(
                    PurePath(document.source_file_name or document.title).suffix
                )
            )
            source: dict[str, Any] = {
                "demo_source_id": document.demo_source_id,
                "canonical_document_id": document.document_id,
                "job_result_id": revision.id,
                "namespace": DEMO_NAMESPACE,
                "title": document.title,
                "category_id": document.category_id,
                "mime_type": mimeType,
                "size_bytes": int(metadata.get("source_size_bytes") or 0),
                "chunk_count": int(count or 0),
                "import_status": state,
                "original_file": {
                    "url": f"/api/v1/demo/sources/{quote(document.demo_source_id, safe='')}/original?job_result_id={revision.id}",
                    "mime_type": mimeType,
                    "size_bytes": int(metadata.get("source_size_bytes") or 0),
                    "can_download": True,
                },
                "examples": metadata.get("bound_demo_examples", []),
            }
            readySources.append(source)
        library: dict[str, Any] = get_official_library_catalog(
            {str(source["demo_source_id"]): source for source in readySources}
        )
        statuses: dict[str, str] = {
            entry["demo_source_id"]: entry["status"] for entry in importStatuses
        }
        for source in library.get("sources", []):
            if source.get("demo_source_id") not in {
                item["demo_source_id"] for item in readySources
            }:
                source["status"] = "planned"
            source["import_status"] = statuses.get(
                str(source.get("demo_source_id") or ""), "planned"
            )
            readySource = next(
                (
                    item
                    for item in readySources
                    if item["demo_source_id"] == source.get("demo_source_id")
                ),
                None,
            )
            if readySource is not None:
                source["job_result_id"] = readySource["job_result_id"]
                source["namespace"] = DEMO_NAMESPACE
        return {
            "namespace": DEMO_NAMESPACE,
            "sources": readySources,
            "import_status": importStatuses,
            "official_library": library,
        }

    async def resolve_source(
        self, db: AsyncSession, *, source_id: str, job_result_id: str | None = None
    ) -> tuple[DemoDocument, JobResult]:
        document: DemoDocument | None = (
            await db.execute(
                select(DemoDocument).where(
                    DemoDocument.demo_source_id == source_id,
                    DemoDocument.status == "active",
                )
            )
        ).scalar_one_or_none()
        if document is None:
            raise NotFoundException(resource="Demo source", resource_id=source_id)
        return await resolve_demo_revision(
            db, document_id=document.document_id, job_result_id=job_result_id
        )

    async def get_media_url(
        self,
        db: AsyncSession,
        *,
        source_id: str,
        job_result_id: str | None = None,
        asset_path: str | None = None,
    ) -> str:
        document, revision = await self.resolve_source(
            db, source_id=source_id, job_result_id=job_result_id
        )
        job: Job = (
            await db.execute(select(Job).where(Job.job_id == revision.job_id))
        ).scalar_one()
        storage: JobFileStorage = JobFileStorage()
        if asset_path is not None:
            signer: DemoAssetSigner = DemoAssetSigner(
                job.job_id, revision.document_metadata or {}
            )
            assetUrl: str | None = await asyncio.to_thread(
                signer.generate_url, asset_path
            )
            if assetUrl is None:
                raise NotFoundException(resource="Demo asset", resource_id=asset_path)
            return assetUrl
        if asset_path is None:
            key: str | None = job.s3_key
            bucket: str = storage.uploads_bucket
            fileName: str = str(
                (job.job_metadata or {}).get("source_file_name")
                or document.source_file_name
                or document.title
            )
        if not key:
            raise NotFoundException(resource="Demo original", resource_id=source_id)
        mimeType: str = JobFileStorage.get_content_type(PurePath(fileName).suffix)
        # Redirects let the storage service handle Range without proxying large
        # original files through an API worker. The GET signature overrides MIME.
        return await asyncio.to_thread(
            storage.storage_adapter.generate_presigned_url,
            key,
            expiration=3600,
            bucket=bucket,
            method="GET",
            headers={
                "Content-Type": mimeType,
                "Content-Disposition": "inline; filename*=UTF-8''"
                + quote(fileName, safe=""),
            },
        )
