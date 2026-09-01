"""Redis cache for compressed namespace MAP routing snapshots."""

from __future__ import annotations

from shared.models.schemas.retrieval_namespace import normalize_retrieval_namespace
from shared.services.redis import RedisServiceFactory

_CACHE_TTL_SECONDS = 3600
_KEY_PREFIX = "retrieval:snapshot:v2"


class NamespaceMapSnapshotRedisCache:
    """Access generation-scoped compressed namespace snapshots in Redis."""

    @staticmethod
    def _build_key(*, user_id: str, namespace: str, generation: int) -> str:
        normalized_namespace = normalize_retrieval_namespace(namespace)
        return f"{_KEY_PREFIX}:{user_id}:{normalized_namespace}:g{int(generation)}"

    @classmethod
    async def get(
        cls, *, user_id: str, namespace: str, generation: int
    ) -> bytes | None:
        service = RedisServiceFactory.get_service()
        return await service.get_bytes(
            cls._build_key(
                user_id=user_id, namespace=namespace, generation=generation
            )
        )

    @classmethod
    async def set(
        cls, *, user_id: str, namespace: str, generation: int, payload_zlib: bytes
    ) -> bool:
        service = RedisServiceFactory.get_service()
        return await service.set_bytes(
            cls._build_key(
                user_id=user_id, namespace=namespace, generation=generation
            ),
            payload_zlib,
            ex=_CACHE_TTL_SECONDS,
        )
