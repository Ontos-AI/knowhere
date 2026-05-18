"""
Sync Redis services for Celery worker (gevent pool).
Under gevent, sync socket operations yield cooperatively via monkey patching.
API service continues using async RedisService.
"""

import json
from typing import Any, Dict, Optional, Sequence, cast

from loguru import logger
from redis import Redis as SyncRedisClient
from redis.connection import BlockingConnectionPool

from shared.core.config.redis import RedisConfigManager
from shared.services.redis.key_builder import RedisKeyType, redis_key_builder


class SyncRedisService:
    """Sync Redis service for gevent worker."""

    _KEY_PREFIX: str = "knowhere-api"

    def __init__(self, config_manager: Optional[RedisConfigManager] = None):
        if config_manager is None:
            from shared.core.config import settings

            config_manager = RedisConfigManager(settings)
        self.config_manager = config_manager
        self._client: Optional[SyncRedisClient] = None

    def _get_client(self) -> SyncRedisClient:
        if self._client is None:
            url = self.config_manager.get_connection_url()
            params = self.config_manager.get_connection_params()
            config = self.config_manager.config
            max_conn = config.REDIS_SYNC_MAX_CONNECTIONS
            pool_timeout = config.REDIS_SYNC_POOL_TIMEOUT
            # Remove keys that BlockingConnectionPool gets from the URL or we set explicitly
            params.pop("host", None)
            params.pop("port", None)
            params.pop("db", None)
            params.pop("password", None)
            params.pop("max_connections", None)
            pool = BlockingConnectionPool.from_url(
                url,
                max_connections=max_conn,
                timeout=pool_timeout,
                **params,
            )
            self._client = SyncRedisClient(connection_pool=pool)
            logger.info(
                f"Sync Redis client initialized "
                f"(pool=BlockingConnectionPool, max_connections={max_conn}, timeout={pool_timeout}s)"
            )
        return self._client

    def pipeline(self, transaction: bool = False):
        """Return a Redis pipeline for batching commands (1 pool checkout)."""
        return self._get_client().pipeline(transaction)

    def _build_key(self, key: str) -> str:
        prefix = self._KEY_PREFIX
        return f"{prefix}:{key}" if not key.startswith(prefix) else key

    def set(
        self, key: str, value: Any, ttl: Optional[int] = None, ex: Optional[int] = None
    ) -> bool:
        try:
            client = self._get_client()
            full_key = self._build_key(key)
            if isinstance(value, (dict, list)):
                from shared.utils.json_utils import make_json_safe

                safe_value = make_json_safe(value)
                value = json.dumps(safe_value, ensure_ascii=False)
            expire_time = ex or ttl or self.config_manager.config.REDIS_DEFAULT_TTL
            return bool(client.set(full_key, value, ex=expire_time))
        except Exception as e:
            logger.error(f"Redis SET failed: key={key}, error={e}")
            raise

    def set_nx(self, key: str, value: str, ex: int) -> bool:
        """Atomic SET NX EX — acquire an idempotency or advisory lock.

        Sets ``key`` to ``value`` with TTL ``ex`` seconds only if the key does
        not already exist (``NX``).  Returns ``True`` if the key was written
        (this caller owns the lock), ``False`` if it already existed.

        Unlike ``set()``, this method does **not** JSON-encode the value and
        does **not** fall back to a default TTL — both are intentional so that
        callers get exactly the semantics they declare.
        """
        try:
            full_key = self._build_key(key)
            return bool(self._get_client().set(full_key, value, nx=True, ex=ex))
        except Exception as e:
            logger.error(f"Redis SET NX failed: key={key}, error={e}")
            raise

    def get(self, key: str, default: Any = None) -> Any:
        try:
            client = self._get_client()
            full_key = self._build_key(key)
            result: Optional[str] = client.get(full_key)  # type: ignore[assignment]
            if result is None:
                return default
            try:
                return json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return result
        except Exception as e:
            logger.error(f"Redis GET failed: key={key}, error={e}")
            raise

    def delete(self, *keys: str) -> int:
        try:
            client = self._get_client()
            full_keys = [self._build_key(key) for key in keys]
            return int(client.delete(*full_keys))  # type: ignore[arg-type]
        except Exception as e:
            logger.error(f"Redis DELETE failed: keys={keys}, error={e}")
            raise

    def exists(self, key: str) -> bool:
        client = self._get_client()
        full_key = self._build_key(key)
        return bool(client.exists(full_key))

    def expire(self, key: str, ttl: int) -> bool:
        client = self._get_client()
        full_key = self._build_key(key)
        return bool(client.expire(full_key, ttl))

    def incr(self, key: str) -> int:
        try:
            client = self._get_client()
            full_key = self._build_key(key)
            return cast(int, client.incr(full_key))
        except Exception as e:
            logger.error(f"Redis INCR failed: key={key}, error={e}")
            raise

    def eval(
        self, script: str, keys: Sequence[str], args: Optional[Sequence[Any]] = None
    ) -> Any:
        """Execute a Lua script with namespaced Redis keys."""
        client = self._get_client()
        full_keys = [self._build_key(key) for key in keys]
        raw_args = list(args or [])
        return client.eval(script, len(full_keys), *(full_keys + raw_args))

    def hset(
        self,
        key: str,
        field: Optional[str] = None,
        value: Any = None,
        mapping: Optional[Dict[str, Any]] = None,
    ) -> int:
        try:
            client = self._get_client()
            full_key = self._build_key(key)
            if mapping is not None:
                serialized_mapping = {}
                for k, v in mapping.items():
                    if isinstance(v, (dict, list)):
                        serialized_mapping[k] = json.dumps(v, ensure_ascii=False)
                    else:
                        serialized_mapping[k] = str(v)
                return int(client.hset(full_key, mapping=serialized_mapping))  # type: ignore[arg-type]
            else:
                serialized_value = value
                if isinstance(value, (dict, list)):
                    serialized_value = json.dumps(value, ensure_ascii=False)
                return int(client.hset(full_key, field, serialized_value))  # type: ignore[arg-type]
        except Exception as e:
            logger.error(f"Redis HSET failed: key={key}, error={e}")
            raise

    def hget(self, key: str, field: str, default: Any = None) -> Any:
        try:
            client = self._get_client()
            full_key = self._build_key(key)
            result: Optional[str] = client.hget(full_key, field)  # type: ignore[assignment]
            if result is None:
                return default
            try:
                return json.loads(result)
            except (json.JSONDecodeError, TypeError):
                return result
        except Exception as e:
            logger.error(f"Redis HGET failed: key={key}, field={field}, error={e}")
            raise

    def hgetall(self, key: str) -> Dict[str, Any]:
        try:
            client = self._get_client()
            full_key = self._build_key(key)
            result: Dict[str, str] = client.hgetall(full_key)  # type: ignore[assignment]
            parsed_result = {}
            for field, value in result.items():
                try:
                    parsed_result[field] = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    parsed_result[field] = value
            return parsed_result
        except Exception as e:
            logger.error(f"Redis HGETALL failed: key={key}, error={e}")
            raise

    def rpush(self, key: str, value: Any) -> int:
        try:
            client = self._get_client()
            full_key = self._build_key(key)
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            return cast(int, client.rpush(full_key, value))
        except Exception as e:
            logger.error(f"Redis RPUSH failed: key={key}, error={e}")
            raise

    def sadd(self, key: str, *values: Any) -> int:
        try:
            client = self._get_client()
            full_key = self._build_key(key)
            serialized: list[str] = []
            for value in values:
                if isinstance(value, (dict, list)):
                    serialized.append(json.dumps(value, ensure_ascii=False))
                else:
                    serialized.append(str(value))
            return cast(int, client.sadd(full_key, *serialized))
        except Exception as e:
            logger.error(f"Redis SADD failed: key={key}, error={e}")
            raise

    def srem(self, key: str, *values: Any) -> int:
        try:
            client = self._get_client()
            full_key = self._build_key(key)
            serialized: list[str] = []
            for value in values:
                if isinstance(value, (dict, list)):
                    serialized.append(json.dumps(value, ensure_ascii=False))
                else:
                    serialized.append(str(value))
            return cast(int, client.srem(full_key, *serialized))
        except Exception as e:
            logger.error(f"Redis SREM failed: key={key}, error={e}")
            raise

    def ping(self) -> bool:
        try:
            client = self._get_client()
            return bool(client.ping())
        except Exception:
            return False

    def close(self):
        if self._client:
            self._client.close()
            self._client = None
            logger.info("Sync Redis client closed")


class SyncRedisServiceFactory:
    """Factory for sync Redis service instances."""

    _instance: Optional[SyncRedisService] = None

    @classmethod
    def get_service(
        cls, config_manager: Optional[RedisConfigManager] = None
    ) -> SyncRedisService:
        if cls._instance is None:
            if config_manager is None:
                from shared.core.config import settings

                config_manager = RedisConfigManager(settings)
            cls._instance = SyncRedisService(config_manager)
        return cls._instance

    @classmethod
    def reset(cls):
        if cls._instance:
            cls._instance.close()
        cls._instance = None


class SyncJobInfoRedisService:
    """Sync Job info Redis service."""

    JOB_INFO_TTL = redis_key_builder.get_key_ttl(RedisKeyType.TASK)

    def __init__(self, redis_service: SyncRedisService):
        self.redis = redis_service

    def save_job_info(self, job_id: str, job_info: Dict[str, Any]) -> bool:
        try:
            key = redis_key_builder.task_info(job_id)
            self.redis.set(key, job_info, ttl=self.JOB_INFO_TTL)
            return True
        except Exception as e:
            logger.error(f"Failed to save job info: {e}")
            return False

    def get_job_info(self, job_id: str) -> Optional[Dict[str, Any]]:
        try:
            key = redis_key_builder.task_info(job_id)
            return self.redis.get(key)
        except Exception as e:
            logger.error(f"Failed to get job info: {e}")
            return None

    def update_job_info(self, job_id: str, updates: Dict[str, Any]) -> bool:
        try:
            job_info = self.get_job_info(job_id)
            if job_info:
                job_info.update(updates)
                return self.save_job_info(job_id, job_info)
            return False
        except Exception as e:
            logger.error(f"Failed to update job info: {e}")
            return False

    def delete_job_info(self, job_id: str) -> bool:
        try:
            key = redis_key_builder.task_info(job_id)
            self.redis.delete(key)
            return True
        except Exception as e:
            logger.error(f"Failed to delete job info: {e}")
            return False


class SyncJobMetadataService:
    """Sync Job metadata Redis service."""

    METADATA_TTL = redis_key_builder.get_key_ttl(RedisKeyType.TASK)

    def __init__(self, redis_service: SyncRedisService):
        self.redis = redis_service

    def save_metadata(self, job_id: str, metadata: Dict[str, Any]) -> bool:
        try:
            key = redis_key_builder.task_metadata(job_id)
            self.redis.set(key, metadata, ttl=self.METADATA_TTL)
            return True
        except Exception as e:
            logger.error(f"Failed to save metadata: {e}")
            return False

    def get_metadata(self, job_id: str) -> Optional[Dict[str, Any]]:
        try:
            key = redis_key_builder.task_metadata(job_id)
            return self.redis.get(key)
        except Exception as e:
            logger.error(f"Failed to get metadata: {e}")
            return None

    def update_metadata(self, job_id: str, updates: Dict[str, Any]) -> bool:
        try:
            metadata = self.get_metadata(job_id)
            if metadata:
                metadata.update(updates)
                return self.save_metadata(job_id, metadata)
            return False
        except Exception as e:
            logger.error(f"Failed to update metadata: {e}")
            return False

    def delete_metadata(self, job_id: str) -> bool:
        try:
            key = redis_key_builder.task_metadata(job_id)
            self.redis.delete(key)
            return True
        except Exception as e:
            logger.error(f"Failed to delete metadata: {e}")
            return False


class SyncTaskRedisService:
    """Sync Task Redis service for worker tasks."""

    def __init__(self, redis_service: SyncRedisService):
        self.redis = redis_service

    def set_task_status(self, task_id: str, status: str) -> bool:
        try:
            task_ttl = redis_key_builder.get_key_ttl(RedisKeyType.TASK)
            status_key = self.redis._build_key(redis_key_builder.task_status(task_id))
            progress_key = self.redis._build_key(
                redis_key_builder.task_progress(task_id)
            )

            pipe = self.redis.pipeline()
            pipe.set(status_key, status, ex=task_ttl)
            pipe.hset(
                progress_key,
                mapping={"status": status, "timestamp": self._get_current_timestamp()},
            )
            pipe.expire(progress_key, task_ttl)
            pipe.execute()
            return True
        except Exception as e:
            logger.error(f"Failed to set task {task_id} status: {e}")
            return False

    def save_task_result(self, task_id: str, result: Dict[str, Any]) -> bool:
        try:
            from shared.utils.json_utils import make_json_safe

            task_ttl = redis_key_builder.get_key_ttl(RedisKeyType.TASK)
            result_key = self.redis._build_key(redis_key_builder.task_result(task_id))
            status_key = self.redis._build_key(redis_key_builder.task_status(task_id))
            progress_key = self.redis._build_key(
                redis_key_builder.task_progress(task_id)
            )
            processing_key = self.redis._build_key(
                redis_key_builder.set_processing_tasks()
            )

            result_json = json.dumps(make_json_safe(result), ensure_ascii=False)
            ts = self._get_current_timestamp()

            pipe = self.redis.pipeline()
            pipe.set(result_key, result_json, ex=task_ttl)
            pipe.set(status_key, "done", ex=task_ttl)
            pipe.hset(progress_key, mapping={"status": "done", "timestamp": ts})
            pipe.expire(progress_key, task_ttl)
            pipe.srem(processing_key, task_id)
            pipe.execute()
            return True
        except Exception as e:
            logger.error(f"Failed to save result for task {task_id}: {e}")
            return False

    def mark_task_failed(self, task_id: str, error_message: str) -> bool:
        try:
            task_ttl = redis_key_builder.get_key_ttl(RedisKeyType.TASK)
            list_ttl = redis_key_builder.get_key_ttl(RedisKeyType.LIST)
            status_key = self.redis._build_key(redis_key_builder.task_status(task_id))
            progress_key = self.redis._build_key(
                redis_key_builder.task_progress(task_id)
            )
            processing_key = self.redis._build_key(
                redis_key_builder.set_processing_tasks()
            )
            error_logs_key = self.redis._build_key(redis_key_builder.list_error_logs())

            status_value = f"failed: {error_message}"
            ts = self._get_current_timestamp()
            error_log_entry = json.dumps(
                {"task_id": task_id, "error": error_message, "timestamp": ts},
                ensure_ascii=False,
            )

            pipe = self.redis.pipeline()
            pipe.set(status_key, status_value, ex=task_ttl)
            pipe.hset(progress_key, mapping={"status": status_value, "timestamp": ts})
            pipe.expire(progress_key, task_ttl)
            pipe.srem(processing_key, task_id)
            pipe.rpush(error_logs_key, error_log_entry)
            pipe.expire(error_logs_key, list_ttl)
            pipe.execute()
            return True
        except Exception as e:
            logger.error(f"Error while marking task {task_id} as failed: {e}")
            return False

    def _get_current_timestamp(self) -> str:
        import time

        return str(int(time.time()))
