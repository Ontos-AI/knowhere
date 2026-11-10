"""Bounded immutable retry input through sync worker or async request I/O."""

import asyncio
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from uuid import uuid4
import zlib

import httpx

from shared.core.config import settings
from shared.core.config.storage import get_cached_storage_adapter
from shared.services.retrieval.lexical.deadline import IndexingDeadline
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.input import LexicalInput
from shared.services.storage.adapters.filesystem_adapter import FileSystemStorageAdapter
from shared.services.storage.async_presigner import AsyncStoragePresigner
from shared.services.storage.storage_adapter import StorageAdapter


@dataclass(frozen=True)
class _Upload:
    key: str
    checksum: str
    data: bytes


class LexicalInputStorage:
    def __init__(self) -> None:
        self.bucket: str = str(getattr(settings, "S3_RESULTS_BUCKET", "") or settings.S3_BUCKET_NAME)
        self.maximumBytes: int = settings.KNOWHERE_INDEXING_INPUT_MAXIMUM_BYTES

    def save(self, *, jobId: str, revisionId: str, payload: LexicalInput) -> tuple[str, str]:
        key, checksum, _ = self.saveWithSize(jobId=jobId, revisionId=revisionId, payload=payload)
        return key, checksum

    def saveWithSize(self, *, jobId: str, revisionId: str, payload: LexicalInput) -> tuple[str, str, int]:
        upload: _Upload = self._encode(jobId, revisionId, payload)
        get_cached_storage_adapter().upload_fileobj(BytesIO(upload.data), upload.key,
            bucket=self.bucket, content_type="application/zlib")
        return upload.key, upload.checksum, len(upload.data)

    def load(self, *, key: str, checksum: str, adapter: StorageAdapter | None = None) -> LexicalInput:
        source: StorageAdapter = adapter if adapter is not None else get_cached_storage_adapter()
        size: int | None = source.get_object_size(key, bucket=self.bucket)
        if size is not None and size > self.maximumBytes:
            raise LexicalIndexError("Retained indexing input exceeds its byte limit", isSizeLimit=True)
        return self._decode(source.download_fileobj(key, bucket=self.bucket), checksum)

    async def saveAsync(self, *, jobId: str, revisionId: str, payload: LexicalInput,
                        deadline: IndexingDeadline) -> tuple[str, str, int]:
        deadline.getRemainingSeconds()
        upload: _Upload = self._encode(jobId, revisionId, payload)
        deadline.getRemainingSeconds()
        if settings.S3_TYPE == "filesystem":
            adapter = get_cached_storage_adapter()
            if not isinstance(adapter, FileSystemStorageAdapter):
                raise LexicalIndexError("Contract storage adapter does not match its configured backend")
            # Contract-only storage: never route cloud I/O through this branch.
            await asyncio.sleep(0)
            adapter.upload_fileobj(BytesIO(upload.data), upload.key, bucket=self.bucket, content_type="application/zlib")
        else:
            headers: dict[str, str] = {"Content-Type": "application/zlib"}
            url: str = await AsyncStoragePresigner().generateUrl(upload.key, bucket=self.bucket,
                method="PUT", headers=headers, deadline=deadline)
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                response: httpx.Response = await client.put(url, content=upload.data, headers=headers,
                    timeout=deadline.getRemainingSeconds())
                response.raise_for_status()
        deadline.getRemainingSeconds()
        return upload.key, upload.checksum, len(upload.data)

    async def loadAsync(self, *, key: str, checksum: str, deadline: IndexingDeadline) -> LexicalInput:
        deadline.getRemainingSeconds()
        if settings.S3_TYPE == "filesystem":
            adapter = get_cached_storage_adapter()
            if not isinstance(adapter, FileSystemStorageAdapter):
                raise LexicalIndexError("Contract storage adapter does not match its configured backend")
            await asyncio.sleep(0)
            data: bytes = adapter.download_fileobj(key, bucket=self.bucket)
        else:
            url: str = await AsyncStoragePresigner().generateUrl(key, bucket=self.bucket, method="GET", deadline=deadline)
            chunks: list[bytes] = []
            size: int = 0
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                async with client.stream("GET", url, timeout=deadline.getRemainingSeconds()) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        deadline.getRemainingSeconds()
                        size += len(chunk)
                        if size > self.maximumBytes:
                            raise LexicalIndexError("Retained indexing input exceeds its byte limit", isSizeLimit=True)
                        chunks.append(chunk)
            data = b"".join(chunks)
        deadline.getRemainingSeconds()
        payload: LexicalInput = self._decode(data, checksum)
        deadline.getRemainingSeconds()
        return payload

    def _encode(self, jobId: str, revisionId: str, payload: LexicalInput) -> _Upload:
        data: bytes = payload.model_dump_json().encode()
        if len(data) > self.maximumBytes:
            raise LexicalIndexError("Prepared indexing input exceeds its byte limit", isSizeLimit=True)
        checksum: str = sha256(data).hexdigest()
        # A later attempt cannot reuse an orphan key already fenced for deletion.
        key: str = f"indexing-input/v1/{jobId}/{revisionId}/{checksum}-{uuid4().hex}.json.zlib"
        return _Upload(key=key, checksum=checksum, data=zlib.compress(data))

    def _decode(self, compressed: bytes, checksum: str) -> LexicalInput:
        if len(compressed) > self.maximumBytes:
            raise LexicalIndexError("Retained indexing input exceeds its byte limit", isSizeLimit=True)
        try:
            decoder = zlib.decompressobj()
            data: bytes = decoder.decompress(compressed, self.maximumBytes + 1)
            if len(data) > self.maximumBytes or decoder.unconsumed_tail:
                raise LexicalIndexError("Expanded indexing input exceeds its byte limit", isSizeLimit=True)
            if not decoder.eof or decoder.unused_data:
                raise LexicalIndexError("Retained indexing input is incomplete or corrupt")
            if sha256(data).hexdigest() != checksum:
                raise LexicalIndexError("Retained indexing input failed its checksum")
            payload: LexicalInput = LexicalInput.model_validate_json(data)
        except (ValueError, zlib.error) as error:
            raise LexicalIndexError("Retained indexing input is invalid") from error
        if payload.formatVersion != 1:
            raise LexicalIndexError("Retained indexing input format is unsupported")
        return payload
