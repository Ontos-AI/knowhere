"""API key authentication workflow."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.repositories.api_key_repository import APIKeyRepository
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.config import redis_pool_manager
from shared.services.redis.redis_service import RedisService
from shared.utils.api_keys import hash_api_key

_API_KEY_USER_CACHE_TTL_SECONDS: int = 3600
_LAST_USED_DEBOUNCE_SECONDS: int = 300


class APIKeyAuthenticationService:
    """Validate API keys and maintain the API-key auth cache."""

    def __init__(
        self,
        *,
        repository: APIKeyRepository | None = None,
    ) -> None:
        self._repository = repository or APIKeyRepository()

    async def validate_api_key(
        self,
        session: AsyncSession,
        api_key: str,
    ) -> str | None:
        """Validate an API key and return the owning user ID."""
        key_hash = hash_api_key(api_key)
        redis_service = redis_pool_manager.get_redis_service()
        cached_user_id = await self._get_cached_user_id(redis_service, key_hash)
        if cached_user_id is not None:
            return cached_user_id

        api_key_record = await self._repository.get_by_key_hash(session, key_hash)
        if not api_key_record or not api_key_record.is_valid():
            return None

        user_id = str(api_key_record.user_id)
        await self._update_last_used_best_effort(
            session,
            redis_service,
            str(api_key_record.id),
        )
        await self._set_cached_user_id(
            redis_service,
            key_hash,
            user_id,
            self._resolve_api_key_cache_ttl_seconds(api_key_record.expires_at),
        )
        return user_id

    async def invalidate_api_key_user_cache(
        self,
        *,
        user_id: str,
        api_key_hash: str,
    ) -> None:
        """Remove one API-key auth cache entry."""
        await self._invalidate_cached_api_key_user_id(
            redis_pool_manager.get_redis_service(),
            user_id,
            api_key_hash,
        )

    @staticmethod
    def _get_user_id_key(api_key_hash: str) -> str:
        return f"api-key:user-id:{api_key_hash}"

    @staticmethod
    def _get_user_api_keys_key(user_id: str) -> str:
        return f"api-key:user-hashes:{user_id}"

    @staticmethod
    def _get_last_used_debounce_key(api_key_id: str) -> str:
        return f"api-key:last-used-debounce:{api_key_id}"

    async def _get_cached_user_id(
        self,
        redis_service: RedisService,
        api_key_hash: str,
    ) -> str | None:
        try:
            raw_user_id = await redis_service.get(self._get_user_id_key(api_key_hash))
            return self._coerce_user_id(raw_user_id)
        except Exception:
            logger.warning("api_key_authentication: failed to read API-key user cache")
            return None

    async def _set_cached_user_id(
        self,
        redis_service: RedisService,
        api_key_hash: str,
        user_id: str,
        ttl_seconds: int,
    ) -> None:
        effective_ttl_seconds = min(_API_KEY_USER_CACHE_TTL_SECONDS, ttl_seconds)
        user_id_key = self._get_user_id_key(api_key_hash)
        user_api_keys_key = self._get_user_api_keys_key(user_id)

        try:
            await redis_service.set(user_id_key, user_id, ttl=effective_ttl_seconds)
            await redis_service.sadd(user_api_keys_key, api_key_hash)
            reverse_ttl_seconds = await redis_service.ttl(user_api_keys_key)
            if (
                reverse_ttl_seconds in (-2, -1)
                or reverse_ttl_seconds < effective_ttl_seconds
            ):
                await redis_service.expire(user_api_keys_key, effective_ttl_seconds)
        except Exception:
            logger.warning(
                "api_key_authentication: failed to write API-key user cache for user_id={}",
                user_id,
            )

    async def _invalidate_cached_api_key_user_id(
        self,
        redis_service: RedisService,
        user_id: str,
        api_key_hash: str,
    ) -> None:
        try:
            await redis_service.delete(self._get_user_id_key(api_key_hash))
            await redis_service.srem(self._get_user_api_keys_key(user_id), api_key_hash)
        except Exception:
            logger.warning(
                "api_key_authentication: failed to invalidate API-key cache for user_id={}",
                user_id,
            )

    def _coerce_user_id(self, raw_user_id: object) -> str | None:
        if isinstance(raw_user_id, str):
            try:
                parsed_user_id: object = json.loads(raw_user_id)
            except json.JSONDecodeError:
                return raw_user_id
        else:
            parsed_user_id = raw_user_id

        if isinstance(parsed_user_id, str):
            return parsed_user_id

        if isinstance(parsed_user_id, dict):
            legacy_user_id = parsed_user_id.get("user_id")
            if isinstance(legacy_user_id, str):
                return legacy_user_id

        return None

    def _resolve_api_key_cache_ttl_seconds(self, expires_at: datetime | None) -> int:
        if expires_at is None:
            return _API_KEY_USER_CACHE_TTL_SECONDS

        expires_at_utc = expires_at
        if expires_at_utc.tzinfo is None:
            expires_at_utc = expires_at_utc.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        remaining_seconds = int((expires_at_utc - now).total_seconds())
        return max(1, min(_API_KEY_USER_CACHE_TTL_SECONDS, remaining_seconds))

    async def _update_last_used_best_effort(
        self,
        session: AsyncSession,
        redis_service: RedisService,
        api_key_id: str,
    ) -> None:
        """Update last_used_at on the request session without a nested checkout.

        Redis SET NX debounce skips redundant writes within the debounce window
        so job polls do not compete for QueuePool via create_task+get_db_context.
        """
        debounce_key = self._get_last_used_debounce_key(api_key_id)
        try:
            acquired = await redis_service.set_nx(
                debounce_key,
                "1",
                ex=_LAST_USED_DEBOUNCE_SECONDS,
            )
            if not acquired:
                return
        except Exception:
            logger.warning(
                "api_key_authentication: last-used debounce failed for api_key_id={}; "
                "updating anyway",
                api_key_id,
            )

        try:
            await self._repository.update_last_used(session, api_key_id)
            # Request-scoped sessions do not auto-commit.
            await session.commit()
        except Exception as exc:
            logger.warning(
                f"Failed to update API key last-used time (ignored): {exc}"
            )
            try:
                await session.rollback()
            except Exception:
                # Rollback itself can fail if the session is already closed;
                # ignore so last-used remains best-effort.
                pass
