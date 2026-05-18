"""Async Redis service abstraction layer."""

import asyncio
import builtins
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar, cast

import redis.asyncio as redis
from loguru import logger

from shared.core.config.redis import RedisConfigManager
from shared.core.exceptions.redis_exceptions import (
    RedisConnectionError,
    RedisOperationError,
)
from shared.services.redis.retry_policy import RedisHealthChecker, RedisRetry

ResponseT = TypeVar("ResponseT")


async def _await_redis_result(
    result: ResponseT | Awaitable[ResponseT],
) -> ResponseT:
    if hasattr(result, "__await__"):
        return await cast(Awaitable[ResponseT], result)
    return cast(ResponseT, result)


class RedisService:
    """Async Redis service abstraction."""

    _KEY_PREFIX: str = "knowhere-api"

    def __init__(self, config_manager: Optional[RedisConfigManager] = None):
        if config_manager is None:
            from shared.core.config import settings

            config_manager = RedisConfigManager(settings)
        self.config_manager = config_manager
        self._client: Optional[redis.Redis] = None
        self._health_checker: Optional[RedisHealthChecker] = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> redis.Redis:
        """Get the Redis client with lazy initialization."""
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    try:
                        self._client = redis.from_url(
                            self.config_manager.get_connection_url(),
                            **self.config_manager.get_connection_params(),
                        )
                        self._health_checker = RedisHealthChecker(self._client)
                        logger.debug("Redis client initialized")
                    except Exception as e:
                        raise RedisConnectionError(
                            internal_message=f"Redis client initialization failed: {str(e)}",
                            original_exception=e,
                        )
        return self._client

    async def _execute_with_retry(
        self, operation: Callable[[], Awaitable[ResponseT]]
    ) -> ResponseT:
        """Execute a Redis operation with retry support."""
        return await RedisRetry.with_retry(
            operation,
            max_retries=self.config_manager.config.REDIS_MAX_RETRIES,
            base_delay=self.config_manager.config.REDIS_RETRY_DELAY,
        )

    def _build_key(self, key: str) -> str:
        """Build the fully-prefixed Redis key."""
        prefix = self._KEY_PREFIX
        return f"{prefix}:{key}" if not key.startswith(prefix) else key

    # ==================== Basic Operations ====================

    async def set(
        self, key: str, value: Any, ttl: Optional[int] = None, ex: Optional[int] = None
    ) -> bool:
        """Set a key value."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            if isinstance(value, (dict, list)):
                # Use make_json_safe so complex types serialize consistently.
                from shared.utils.json_utils import make_json_safe

                safe_value = make_json_safe(value)
                value = json.dumps(safe_value, ensure_ascii=False)
                logger.debug(
                    f"Redis serialization completed: key={full_key}, type={type(value)}"
                )

            # Prefer ex when provided, then ttl, then the default config TTL.
            expire_time = ex or ttl or self.config_manager.config.REDIS_DEFAULT_TTL

            async def _operation():
                return await client.set(full_key, value, ex=expire_time)

            result = await self._execute_with_retry(_operation)
            return result
        except Exception as e:
            logger.error(f"Redis SET operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"SET operation failed: {str(e)}",
                operation="SET",
                original_exception=e,
            )

    async def get(self, key: str, default: Any = None) -> Any:
        """Get a key value."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            async def _operation():
                return await client.get(full_key)

            result = await self._execute_with_retry(_operation)

            if result is None:
                return default

            # Try decoding JSON payloads.
            try:
                return json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return result
        except Exception as e:
            logger.error(f"Redis GET operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"GET operation failed: {str(e)}",
                operation="GET",
                original_exception=e,
            )

    async def delete(self, *keys: str) -> int:
        """Delete keys."""
        try:
            client = await self._get_client()
            full_keys = [self._build_key(key) for key in keys]

            async def _operation():
                return await client.delete(*full_keys)

            return await self._execute_with_retry(_operation)
        except Exception as e:
            logger.error(f"Redis DELETE operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"DELETE operation failed: {str(e)}",
                operation="DELETE",
                original_exception=e,
            )

    async def exists(self, key: str) -> bool:
        """Check whether a key exists."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            async def _operation():
                return await client.exists(full_key)

            result = await self._execute_with_retry(_operation)
            return bool(result)
        except Exception as e:
            logger.error(f"Redis EXISTS operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"EXISTS operation failed: {str(e)}",
                operation="EXISTS",
                original_exception=e,
            )

    async def expire(self, key: str, ttl: int) -> bool:
        """Set a key TTL."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            async def _operation():
                return await client.expire(full_key, ttl)

            return await self._execute_with_retry(_operation)
        except Exception as e:
            logger.error(f"Redis EXPIRE operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"EXPIRE operation failed: {str(e)}",
                operation="EXPIRE",
                original_exception=e,
            )

    async def ttl(self, key: str) -> int:
        """Get the remaining TTL for a key in seconds."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            async def _operation():
                return await client.ttl(full_key)

            result = await self._execute_with_retry(_operation)
            return int(result)
        except Exception as e:
            logger.error(f"Redis TTL operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"TTL operation failed: {str(e)}",
                operation="TTL",
                original_exception=e,
            )

    # ==================== List Operations ====================

    async def lpush(self, key: str, *values: Any) -> int:
        """Push elements onto the left side of a list."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            # Serialize values before writing them.
            serialized_values = []
            for value in values:
                if isinstance(value, (dict, list)):
                    serialized_values.append(json.dumps(value, ensure_ascii=False))
                else:
                    serialized_values.append(str(value))

            async def _operation():
                return await _await_redis_result(client.lpush(full_key, *serialized_values))

            return await self._execute_with_retry(_operation)
        except Exception as e:
            logger.error(f"Redis LPUSH operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"LPUSH operation failed: {str(e)}",
                operation="LPUSH",
                original_exception=e,
            )

    async def rpush(self, key: str, *values: Any) -> int:
        """Push elements onto the right side of a list."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            # Serialize values before writing them.
            serialized_values = []
            for value in values:
                if isinstance(value, (dict, list)):
                    serialized_values.append(json.dumps(value, ensure_ascii=False))
                else:
                    serialized_values.append(str(value))

            async def _operation():
                return await _await_redis_result(client.rpush(full_key, *serialized_values))

            return await self._execute_with_retry(_operation)
        except Exception as e:
            logger.error(f"Redis RPUSH operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"RPUSH operation failed: {str(e)}",
                operation="RPUSH",
                original_exception=e,
            )

    async def lpop(self, key: str) -> Any:
        """Pop an element from the left side of a list."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            async def _operation():
                return await _await_redis_result(client.lpop(full_key))

            result = await self._execute_with_retry(_operation)

            if result is None:
                return None

            # Try decoding JSON payloads.
            if isinstance(result, (str, bytes, bytearray)):
                try:
                    return json.loads(result)
                except json.JSONDecodeError:
                    return result
            return result
        except Exception as e:
            logger.error(f"Redis LPOP operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"LPOP operation failed: {str(e)}",
                operation="LPOP",
                original_exception=e,
            )

    async def rpop(self, key: str) -> Any:
        """Pop an element from the right side of a list."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            async def _operation():
                return await _await_redis_result(client.rpop(full_key))

            result = await self._execute_with_retry(_operation)

            if result is None:
                return None

            # Try decoding JSON payloads.
            if isinstance(result, (str, bytes, bytearray)):
                try:
                    return json.loads(result)
                except json.JSONDecodeError:
                    return result
            return result
        except Exception as e:
            logger.error(f"Redis RPOP operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"RPOP operation failed: {str(e)}",
                operation="RPOP",
                original_exception=e,
            )

    async def lrange(self, key: str, start: int = 0, end: int = -1) -> List[Any]:
        """Get elements from a list range."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            async def _operation():
                return await _await_redis_result(client.lrange(full_key, start, end))

            result = await self._execute_with_retry(_operation)

            # Try decoding each element as JSON.
            parsed_result = []
            for item in result:
                try:
                    parsed_result.append(json.loads(item))
                except (json.JSONDecodeError, TypeError):
                    parsed_result.append(item)

            return parsed_result
        except Exception as e:
            logger.error(f"Redis LRANGE operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"LRANGE operation failed: {str(e)}",
                operation="LRANGE",
                original_exception=e,
            )

    # ==================== Hash Operations ====================

    async def hset(
        self,
        key: str,
        field: str | None = None,
        value: Any = None,
        mapping: Dict[str, Any] | None = None,
    ) -> int:
        """Set one or more hash fields."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            async def _operation():
                if mapping is not None:
                    # Serialize mapping values before writing them.
                    serialized_mapping = {}
                    for k, v in mapping.items():
                        if isinstance(v, (dict, list)):
                            serialized_mapping[k] = json.dumps(v, ensure_ascii=False)
                        else:
                            serialized_mapping[k] = str(v)
                    return await _await_redis_result(
                        client.hset(full_key, mapping=serialized_mapping)
                    )
                else:
                    # Handle a single field value.
                    serialized_value = value
                    if isinstance(value, (dict, list)):
                        serialized_value = json.dumps(value, ensure_ascii=False)
                    return await _await_redis_result(
                        client.hset(full_key, field, serialized_value)
                    )

            return await self._execute_with_retry(_operation)
        except Exception as e:
            logger.error(f"Redis HSET operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"HSET operation failed: {str(e)}",
                operation="HSET",
                original_exception=e,
            )

    async def hget(self, key: str, field: str, default: Any = None) -> Any:
        """Get a hash field value."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            async def _operation():
                return await _await_redis_result(client.hget(full_key, field))

            result = await self._execute_with_retry(_operation)

            if result is None:
                return default

            # Try decoding JSON payloads.
            try:
                return json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return result
        except Exception as e:
            logger.error(f"Redis HGET operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"HGET operation failed: {str(e)}",
                operation="HGET",
                original_exception=e,
            )

    async def hgetall(self, key: str) -> Dict[str, Any]:
        """Get all hash fields."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            async def _operation():
                return await _await_redis_result(client.hgetall(full_key))

            result = await self._execute_with_retry(_operation)

            # Try decoding each value as JSON.
            parsed_result = {}
            for field, value in result.items():
                try:
                    parsed_result[field] = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    parsed_result[field] = value

            return parsed_result
        except Exception as e:
            logger.error(f"Redis HGETALL operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"HGETALL operation failed: {str(e)}",
                operation="HGETALL",
                original_exception=e,
            )

    # ==================== Set Operations ====================

    async def sadd(self, key: str, *values: Any) -> int:
        """Add elements to a set."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            # Serialize values before writing them.
            serialized_values = []
            for value in values:
                if isinstance(value, (dict, list)):
                    serialized_values.append(json.dumps(value, ensure_ascii=False))
                else:
                    serialized_values.append(str(value))

            async def _operation():
                return await _await_redis_result(client.sadd(full_key, *serialized_values))

            return await self._execute_with_retry(_operation)
        except Exception as e:
            logger.error(f"Redis SADD operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"SADD operation failed: {str(e)}",
                operation="SADD",
                original_exception=e,
            )

    async def srem(self, key: str, *values: Any) -> int:
        """Remove elements from a set."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            # Serialize values before writing them.
            serialized_values = []
            for value in values:
                if isinstance(value, (dict, list)):
                    serialized_values.append(json.dumps(value, ensure_ascii=False))
                else:
                    serialized_values.append(str(value))

            async def _operation():
                return await _await_redis_result(client.srem(full_key, *serialized_values))

            return await self._execute_with_retry(_operation)
        except Exception as e:
            logger.error(f"Redis SREM operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"SREM operation failed: {str(e)}",
                operation="SREM",
                original_exception=e,
            )

    async def smembers(self, key: str) -> builtins.set[Any]:
        """Get all set members."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            async def _operation():
                return await _await_redis_result(client.smembers(full_key))

            result = await self._execute_with_retry(_operation)
            return result
        except Exception as e:
            logger.error(f"Redis SMEMBERS operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"SMEMBERS operation failed: {str(e)}",
                operation="SMEMBERS",
                original_exception=e,
            )

    async def llen(self, key: str) -> int:
        """Get the length of a list."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            async def _operation():
                return await _await_redis_result(client.llen(full_key))

            return await self._execute_with_retry(_operation)
        except Exception as e:
            logger.error(f"Redis LLEN operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"LLEN operation failed: {str(e)}",
                operation="LLEN",
                original_exception=e,
            )

    async def incr(self, key: str) -> int:
        """Increment a counter."""
        try:
            client = await self._get_client()
            full_key = self._build_key(key)

            async def _operation():
                return await client.incr(full_key)

            return await self._execute_with_retry(_operation)
        except Exception as e:
            logger.error(f"Redis INCR operation failed: {e}")
            raise RedisOperationError(
                internal_message=f"INCR operation failed: {str(e)}",
                operation="INCR",
                original_exception=e,
            )

    # ==================== Health Checks ====================

    async def ping(self) -> bool:
        """Ping Redis."""
        try:
            client = await self._get_client()

            async def _operation():
                return await client.ping()

            result = await self._execute_with_retry(_operation)
            return result
        except Exception as e:
            logger.error(f"Redis PING failed: {e}")
            return False

    async def is_healthy(self) -> bool:
        """Check whether Redis is healthy."""
        if self._health_checker:
            return await self._health_checker.is_healthy()
        return await self.ping()

    # ==================== Connection Management ====================

    async def close(self):
        """Close the Redis connection."""
        if self._client:
            close_client = cast(
                Callable[[], Awaitable[None]] | None,
                getattr(self._client, "aclose", None),
            )

            if close_client is not None:
                await close_client()
            else:
                await self._client.close()

            self._client = None
            self._health_checker = None
            logger.info("Redis connection closed")

    async def __aenter__(self):
        """Enter the async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit the async context manager."""
        await self.close()
