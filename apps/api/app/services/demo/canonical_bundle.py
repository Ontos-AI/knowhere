"""Publish immutable demo artifacts once per source content version."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
import time
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from loguru import logger

from app.services.demo.canonical_bundle_result import CanonicalDemoBundle
from shared.services.redis.redis_service import RedisService
from shared.services.storage.job_file_storage import JobFileStorage
from shared.services.storage.result_storage import (
    ResultStorage,
    UploadedResultBundle,
    get_result_storage,
)
from shared.services.storage.storage_adapter import StorageAdapter

_READY_FILE = ".bundle-ready.json"
_LEASE_SECONDS = 180
_WAIT_SECONDS = 600
_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
_SourceSignature = tuple[tuple[str, int, int, int], ...]


class CanonicalDemoBundleStore:
    """Own versioning, upload coordination and the durable completion marker."""

    def __init__(self, *, redis_service: RedisService) -> None:
        self._redis: RedisService = redis_service
        self._storage: ResultStorage = get_result_storage()
        self._files: JobFileStorage = JobFileStorage()

    async def ensure_bundle(
        self, *, source_id: str, source_directory: Path
    ) -> CanonicalDemoBundle:
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", source_id) is None:
            raise ValueError("Invalid canonical demo source identifier")
        directory: Path = source_directory.resolve(strict=True)
        signature: _SourceSignature = await asyncio.to_thread(
            _read_source_signature, directory
        )
        version: str = await asyncio.to_thread(
            _calculate_content_version, directory, signature
        )
        storage_id: str = f"demo-canonical/{source_id}/{version}"
        bundle: CanonicalDemoBundle | None = await asyncio.to_thread(
            self._read_ready_bundle, storage_id, version
        )
        if bundle is not None:
            return bundle

        lock_key: str = f"lock:demo_bundle:{source_id}:{version}"
        owner: str = uuid4().hex
        deadline: float = time.monotonic() + _WAIT_SECONDS
        while not await self._redis.set_nx(lock_key, owner, ex=_LEASE_SECONDS):
            bundle = await asyncio.to_thread(
                self._read_ready_bundle, storage_id, version
            )
            if bundle is not None:
                return bundle
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for canonical demo artifacts")
            await asyncio.sleep(0.5)

        stopped: asyncio.Event = asyncio.Event()
        renewal: asyncio.Task[None] = asyncio.create_task(
            self._renew_lease(lock_key, owner, stopped)
        )
        try:
            # A previous owner can finish between the first read and SET NX.
            bundle = await asyncio.to_thread(
                self._read_ready_bundle, storage_id, version
            )
            if bundle is not None:
                return bundle
            upload: asyncio.Task[CanonicalDemoBundle] = asyncio.create_task(
                asyncio.to_thread(
                    self._upload_bundle, storage_id, version, directory, signature
                )
            )
            try:
                uploaded_bundle: CanonicalDemoBundle = await asyncio.shield(upload)
            except asyncio.CancelledError:
                # A thread cannot be cancelled; retain the lease until it stops.
                await asyncio.gather(upload)
                raise
            if renewal.done():
                renewal.result()
            if not await self._redis.eval(
                _RENEW_SCRIPT, keys=[lock_key], args=[owner, _LEASE_SECONDS]
            ):
                raise RuntimeError("Canonical demo upload lease was lost")
            marker_upload: asyncio.Task[None] = asyncio.create_task(
                asyncio.to_thread(self._write_ready_marker, uploaded_bundle)
            )
            try:
                await asyncio.shield(marker_upload)
            except asyncio.CancelledError:
                await asyncio.gather(marker_upload)
                raise
            return uploaded_bundle
        finally:
            stopped.set()
            try:
                await renewal
            except Exception as error:
                logger.warning("Canonical demo lease renewal failed: {}", error)
            try:
                await self._redis.eval(_RELEASE_SCRIPT, keys=[lock_key], args=[owner])
            except Exception as error:
                # Ownership expires even if Redis is unavailable during cleanup.
                logger.warning("Canonical demo lease release failed: {}", error)

    async def _renew_lease(
        self, lock_key: str, owner: str, stopped: asyncio.Event
    ) -> None:
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=_LEASE_SECONDS / 3)
            except TimeoutError:
                if not await self._redis.eval(
                    _RENEW_SCRIPT, keys=[lock_key], args=[owner, _LEASE_SECONDS]
                ):
                    raise RuntimeError("Canonical demo upload lease was lost")

    def _read_ready_bundle(
        self, storage_id: str, version: str
    ) -> CanonicalDemoBundle | None:
        raw_prefix: str = self._files.build_result_raw_prefix(job_id=storage_id)
        marker_key: str = f"{raw_prefix}{_READY_FILE}"
        adapter: StorageAdapter = self._files.storage_adapter
        bucket: str = self._files.results_bucket
        if not adapter.exists(marker_key, bucket):
            return None
        try:
            marker: object = json.loads(adapter.download_fileobj(marker_key, bucket))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(marker, dict) or marker.get("content_version") != version:
            return None
        zip_size: object = marker.get("zip_size")
        if type(zip_size) is not int or zip_size <= 0:
            return None
        zip_key: str = self._files.build_result_zip_key(job_id=storage_id)
        if adapter.get_object_size(zip_key, bucket) != zip_size:
            return None
        return CanonicalDemoBundle(
            zip_key=zip_key,
            zip_size=zip_size,
            raw_prefix=raw_prefix,
            content_version=version,
            reused=True,
        )

    def _upload_bundle(
        self,
        storage_id: str,
        version: str,
        directory: Path,
        signature: _SourceSignature,
    ) -> CanonicalDemoBundle:
        marker_key: str = (
            f"{self._files.build_result_raw_prefix(job_id=storage_id)}{_READY_FILE}"
        )
        if self._files.storage_adapter.exists(marker_key, self._files.results_bucket):
            # A malformed marker or missing ZIP must stay incomplete during repair.
            self._files.delete_object(marker_key, bucket=self._files.results_bucket)
        with tempfile.TemporaryDirectory(prefix="knowhere-demo-result-") as temporary:
            archive_path: Path = Path(
                shutil.make_archive(
                    str(Path(temporary) / "bundle"), "zip", root_dir=directory
                )
            )
            zip_size: int = archive_path.stat().st_size
            uploaded: UploadedResultBundle = self._storage.upload(
                job_id=storage_id,
                result_dir=str(directory),
                zip_file_path=str(archive_path),
            )
        if _read_source_signature(directory) != signature:
            raise RuntimeError("Canonical demo source changed during artifact upload")
        return CanonicalDemoBundle(
            zip_key=uploaded.zip_key,
            zip_size=zip_size,
            raw_prefix=uploaded.raw_prefix,
            content_version=version,
            reused=False,
        )

    def _write_ready_marker(self, bundle: CanonicalDemoBundle) -> None:
        # The marker is the commit point; a ZIP alone is never a complete bundle.
        payload: bytes = json.dumps(
            {"content_version": bundle.content_version, "zip_size": bundle.zip_size}
        ).encode("utf-8")
        self._files.upload_fileobj(
            BytesIO(payload),
            f"{bundle.raw_prefix}{_READY_FILE}",
            bucket=self._files.results_bucket,
            content_type="application/json",
        )


def _read_source_signature(directory: Path) -> _SourceSignature:
    files: list[tuple[str, int, int, int]] = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Canonical demo sources cannot contain symbolic links")
        if path.is_file():
            status = path.stat()
            files.append(
                (
                    path.relative_to(directory).as_posix(),
                    status.st_size,
                    status.st_mtime_ns,
                    status.st_ctime_ns,
                )
            )
    if not files:
        raise ValueError("Canonical demo source directory is empty")
    return tuple(files)


@lru_cache(maxsize=128)
def _calculate_content_version(directory: Path, signature: _SourceSignature) -> str:
    # Stat changes invalidate the local memo; the durable key uses actual bytes.
    digest = sha256(b"knowhere-demo-bundle-v1\0")
    for relative_path, size, _modified, _changed in signature:
        path_bytes: bytes = relative_path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(size.to_bytes(8, "big"))
        with (directory / relative_path).open("rb") as source_file:
            while block := source_file.read(1024 * 1024):
                digest.update(block)
    if _read_source_signature(directory) != signature:
        raise RuntimeError("Canonical demo source changed while computing its version")
    return digest.hexdigest()
