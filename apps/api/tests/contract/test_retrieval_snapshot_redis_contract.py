"""Contract tests for binary namespace snapshot caching."""

from __future__ import annotations

import pytest
from sqlalchemy import Executable

from shared.core.config.redis import RedisConfig, RedisConfigManager
from shared.services.redis.redis_service_factory import RedisServiceFactory
from shared.services.redis.redis_service import RedisService
from shared.services.retrieval.nav_snapshot import _resolve_namespace_snapshot_entries
from shared.services.retrieval.namespace_map_snapshot_redis import (
    NamespaceMapSnapshotRedisCache,
)
from shared.services.retrieval.serving_manifest import encode_namespace_map_snapshot


class _FakeRedisClient:
    def __init__(self, *, decode_responses: bool) -> None:
        self.decode_responses = decode_responses
        self.values: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def set(self, key: str, value: bytes, *, ex: int) -> bool:
        self.values[key] = value
        self.ttls[key] = ex
        return True

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_binary_redis_operations_preserve_compressed_snapshot_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[_FakeRedisClient] = []

    def create_client(*args: object, **kwargs: object) -> _FakeRedisClient:
        client = _FakeRedisClient(
            decode_responses=bool(kwargs.get("decode_responses"))
        )
        clients.append(client)
        return client

    monkeypatch.setattr(
        "shared.services.redis.redis_service.redis.from_url", create_client
    )
    service = RedisService(RedisConfigManager(RedisConfig()))
    payload = b"\x78\x9c\x00\xffcompressed-snapshot"

    assert await service.set_bytes("contract:snapshot", payload, ex=3600)
    assert await service.get_bytes("contract:snapshot") == payload
    assert len(clients) == 1
    assert clients[0].decode_responses is False
    assert clients[0].ttls["knowhere-api:contract:snapshot"] == 3600

    await service.close()


@pytest.mark.asyncio
async def test_snapshot_cache_scopes_reads_and_writes_by_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSnapshotService:
        def __init__(self) -> None:
            self.get_keys: list[str] = []
            self.set_calls: list[tuple[str, bytes, int]] = []

        async def get_bytes(self, key: str) -> bytes | None:
            self.get_keys.append(key)
            return b"snapshot"

        async def set_bytes(self, key: str, value: bytes, *, ex: int) -> bool:
            self.set_calls.append((key, value, ex))
            return True

    fake_service = _FakeSnapshotService()
    monkeypatch.setattr(
        RedisServiceFactory,
        "get_service",
        classmethod(lambda cls: fake_service),
    )

    assert (
        await NamespaceMapSnapshotRedisCache.get(
            user_id="user",
            namespace=" ",
            generation=7,
        )
        == b"snapshot"
    )
    assert await NamespaceMapSnapshotRedisCache.set(
        user_id="user",
        namespace="default",
        generation=8,
        payload_zlib=b"compressed",
    )

    assert fake_service.get_keys == ["retrieval:snapshot:v2:user:default:g7"]
    assert fake_service.set_calls == [
        (
            "retrieval:snapshot:v2:user:default:g8",
            b"compressed",
            3600,
        )
    ]


class _SnapshotResult:
    def __init__(self, *, scalar: object = None, row: tuple[object, ...] | None = None):
        self._scalar = scalar
        self._row = row

    def scalar_one_or_none(self) -> object:
        return self._scalar

    def first(self) -> tuple[object, ...] | None:
        return self._row


class _SnapshotSequenceSession:
    def __init__(self, results: list[_SnapshotResult]) -> None:
        self._results = iter(results)

    async def execute(self, _statement: Executable) -> _SnapshotResult:
        return next(self._results)

    async def rollback(self) -> None:
        return None


@pytest.mark.asyncio
async def test_corrupt_redis_blob_falls_back_to_postgres_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload_zlib, checksum, format_version = encode_namespace_map_snapshot(
        {
            "documents": {
                "doc-a": {
                    "job_result_id": "result-a",
                    "job_id": "job-a",
                    "sections": [],
                    "chunks": [],
                }
            }
        }
    )
    set_calls: list[bytes] = []

    async def get_corrupt_blob(**_: object) -> bytes:
        return b"corrupt"

    async def record_repaired_blob(**kwargs: object) -> bool:
        set_calls.append(bytes(kwargs["payload_zlib"]))
        return True

    monkeypatch.setattr(NamespaceMapSnapshotRedisCache, "get", get_corrupt_blob)
    monkeypatch.setattr(NamespaceMapSnapshotRedisCache, "set", record_repaired_blob)
    session = _SnapshotSequenceSession(
        [
            _SnapshotResult(scalar=3),
            _SnapshotResult(row=(3, checksum, format_version)),
            _SnapshotResult(row=(payload_zlib,)),
        ]
    )

    entries = await _resolve_namespace_snapshot_entries(
        session,
        user_id="user",
        namespace="default",
        document_revisions=[("doc-a", "result-a")],
        expected_generation=3,
    )

    assert entries is not None
    assert entries[0][0:2] == ("doc-a", "result-a")
    assert set_calls == [payload_zlib]
