"""Authorize immutable snapshot metadata and refresh exact object capabilities."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from shared.core.exceptions.domain_exceptions import (
    ConflictException,
    NotFoundException,
)
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.schemas.progressive_ingestion import StoredSnapshot
from shared.services.jobs.progressive_ingestion import read_progressive_state
from shared.services.storage.job_file_storage import JobFileStorage

_CORE_FILES: frozenset[str] = frozenset(
    {"manifest.json", "chunks.json", "doc_nav.json", "source.pdf"}
)
_READING_FILES: frozenset[str] = _CORE_FILES | {"full.md", "toc_hierarchies.json"}
_RESOURCE_DIRECTORIES: frozenset[str] = frozenset(
    {"images", "tables", "page_pdfs", "page_citation_assets"}
)


class _SnapshotObject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = Field(min_length=1)
    kind: Literal["essential", "image"]

    @model_validator(mode="after")
    def validate_readable_path(self) -> Self:
        components: list[str] = self.path.split("/")
        if (
            "\\" in self.path
            or any(component in {"", ".", ".."} for component in components)
            or (
                self.path not in _READING_FILES
                and (len(components) < 2 or components[0] not in _RESOURCE_DIRECTORIES)
            )
            or (self.kind == "image") != self.content_type.startswith("image/")
            or (self.path in _CORE_FILES and self.kind != "essential")
        ):
            raise ValueError("Snapshot contains an invalid readable object")
        return self


class _SnapshotInventory(BaseModel):
    version: Literal["1.0"]
    archive_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    objects: list[_SnapshotObject]

    @model_validator(mode="after")
    def validate_required_objects(self) -> Self:
        paths: set[str] = {item.path for item in self.objects}
        if len(paths) != len(self.objects) or not _CORE_FILES <= paths:
            raise ValueError(
                "Snapshot inventory must contain unique readable core objects"
            )
        return self


class SnapshotDeliveryService:
    """Read only published snapshot objects, independently of the live revision."""

    def __init__(self, *, storage: JobFileStorage | None = None) -> None:
        self._storage: JobFileStorage = storage or JobFileStorage()

    async def get_readable_snapshot(
        self,
        database: AsyncSession,
        *,
        user_id: str,
        job_id: str,
        snapshot_id: str,
    ) -> dict[str, object]:
        snapshot, inventory = await self._resolve_snapshot(
            database,
            user_id=user_id,
            job_id=job_id,
            snapshot_id=snapshot_id,
        )
        return {
            **self._build_identity(snapshot),
            "objects": [item.model_dump() for item in inventory.objects],
        }

    async def get_object_capability(
        self,
        database: AsyncSession,
        *,
        user_id: str,
        job_id: str,
        snapshot_id: str,
        path: str,
    ) -> dict[str, object]:
        snapshot, inventory = await self._resolve_snapshot(
            database,
            user_id=user_id,
            job_id=job_id,
            snapshot_id=snapshot_id,
        )
        if not any(item.path == path for item in inventory.objects):
            raise NotFoundException(resource="Snapshot object", resource_id=snapshot_id)
        storage_key: str = f"{self._build_raw_prefix(snapshot)}{path}"
        if not await asyncio.to_thread(
            self._storage.storage_adapter.exists,
            storage_key,
            self._storage.results_bucket,
        ):
            raise NotFoundException(resource="Snapshot object", resource_id=snapshot_id)
        capability: dict[str, object] = await asyncio.to_thread(
            self._sign_object_capability,
            raw_prefix=self._build_raw_prefix(snapshot),
            path=path,
        )
        return {**self._build_identity(snapshot), **capability}

    async def get_object_capabilities(
        self,
        database: AsyncSession,
        *,
        user_id: str,
        job_id: str,
        snapshot_id: str,
        paths: list[str],
    ) -> dict[str, object]:
        snapshot, inventory = await self._resolve_snapshot(
            database,
            user_id=user_id,
            job_id=job_id,
            snapshot_id=snapshot_id,
        )
        inventory_paths: set[str] = {item.path for item in inventory.objects}
        if any(path not in inventory_paths for path in paths):
            raise NotFoundException(resource="Snapshot object", resource_id=snapshot_id)
        # Published immutable inventories declare successfully uploaded objects.
        # Signing does not probe availability: object reads still detect deletion
        # or corruption, and a per-object HEAD cannot prevent a deletion race.
        capabilities: list[dict[str, object]] = await asyncio.to_thread(
            self._sign_object_capabilities,
            raw_prefix=self._build_raw_prefix(snapshot),
            paths=paths,
        )
        return {**self._build_identity(snapshot), "objects": capabilities}

    def _sign_object_capabilities(
        self, *, raw_prefix: str, paths: list[str]
    ) -> list[dict[str, object]]:
        return [
            self._sign_object_capability(raw_prefix=raw_prefix, path=path)
            for path in paths
        ]

    def _sign_object_capability(
        self, *, raw_prefix: str, path: str
    ) -> dict[str, object]:
        capability = self._storage.generate_download_url(
            f"{raw_prefix}{path}", bucket=self._storage.results_bucket
        )
        expires_at: datetime = datetime.now(timezone.utc) + timedelta(
            seconds=int(capability.get("expires_in", 3600))
        )
        return {
            "path": path,
            "download_url": capability["download_url"],
            "expires_at": expires_at.isoformat(),
        }

    async def _resolve_snapshot(
        self,
        database: AsyncSession,
        *,
        user_id: str,
        job_id: str,
        snapshot_id: str,
    ) -> tuple[StoredSnapshot, _SnapshotInventory]:
        job: Job | None = (
            await database.execute(
                select(Job).where(Job.job_id == job_id, Job.user_id == user_id)
            )
        ).scalar_one_or_none()
        if job is None:
            raise NotFoundException(resource="Job", resource_id=job_id)
        result: JobResult | None = (
            await database.execute(
                select(JobResult).options(noload("*")).where(JobResult.job_id == job_id)
            )
        ).scalar_one_or_none()
        state = read_progressive_state(result.document_metadata if result else None)
        snapshot: StoredSnapshot | None = (
            next(
                (item for item in state.snapshots if item.snapshot_id == snapshot_id),
                None,
            )
            if state is not None
            else None
        )
        if (
            result is None
            or snapshot is None
            or (
                snapshot.job_id != job_id
                or snapshot.job_result_id != result.id
                or snapshot.document_id
                != (result.document_id or result.demo_document_id)
            )
        ):
            raise NotFoundException(resource="Snapshot", resource_id=snapshot_id)
        inventory_key: str = f"{self._build_raw_prefix(snapshot)}snapshot-objects.json"
        adapter = self._storage.storage_adapter
        if not await asyncio.to_thread(
            adapter.exists, inventory_key, self._storage.results_bucket
        ):
            # Historical immutable archives retain their existing full-download path.
            raise NotFoundException(
                resource="Readable snapshot", resource_id=snapshot_id
            )
        inventory_bytes: bytes = await asyncio.to_thread(
            adapter.download_fileobj, inventory_key, self._storage.results_bucket
        )
        try:
            inventory = _SnapshotInventory.model_validate_json(inventory_bytes)
            if inventory.archive_checksum_sha256 != snapshot.checksum:
                raise ValueError(
                    "Snapshot inventory checksum does not match publication"
                )
        except (ValidationError, ValueError) as error:
            raise ConflictException(
                user_message="Snapshot delivery metadata failed integrity validation.",
                resource="Snapshot",
                resource_id=snapshot_id,
            ) from error
        return snapshot, inventory

    @staticmethod
    def _build_identity(snapshot: StoredSnapshot) -> dict[str, object]:
        return snapshot.model_dump(exclude={"archive_key", "stage"})

    @staticmethod
    def _build_raw_prefix(snapshot: StoredSnapshot) -> str:
        expected_key: str = (
            f"results/{snapshot.job_id}/snapshots/{snapshot.snapshot_id}.zip"
        )
        if snapshot.archive_key != expected_key:
            raise NotFoundException(
                resource="Readable snapshot", resource_id=snapshot.snapshot_id
            )
        return expected_key.removesuffix(".zip") + "/"
