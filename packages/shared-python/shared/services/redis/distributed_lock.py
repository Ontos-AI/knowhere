"""
Distributed Redis lock for exclusive job processing.

Prevents concurrent processing of the same job when the broker redelivers
tasks while ``task_acks_late=True`` is enabled.

Uses ``SET NX EX`` for atomic acquisition, a Lua script for owner-only
release, and a gevent greenlet for periodic TTL renewal.
"""

import uuid
from typing import Optional

import gevent
import gevent.event
from loguru import logger

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import UnavailableException
from shared.services.redis.redis_sync_service import SyncRedisService
from shared.utils.redis_key_builder import redis_key_builder

# Lua script: atomically check owner token then delete.
# Prevents a stale owner from accidentally deleting a lock
# that was re-acquired by another worker after TTL expiry.
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# Lua script: atomically check owner token then renew TTL.
# Prevents extending a lock that expired and was re-acquired
# by another worker between GET and EXPIRE.
_RENEW_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
else
    return 0
end
"""


class RedisJobLock:
    """Context manager for exclusive job processing via Redis.

    Acquire with ``SET lock_key token NX EX ttl``.  A background gevent
    greenlet renews the TTL every *renewal_interval* seconds so long-running
    tasks (up to the Celery soft time limit) keep the lock alive.

    On exit the lock is released via a Lua script that only deletes the key
    if the stored token still matches (owner-only release).

    Usage::

        with RedisJobLock(redis_service, job_id):
            # only one worker runs this block per job_id
            ...
    """

    def __init__(
        self,
        redis_service: SyncRedisService,
        job_id: str,
        ttl: int = 60,
        renewal_interval: int = 20,
    ) -> None:
        self._redis = redis_service
        self._job_id = job_id
        self._ttl = ttl
        self._renewal_interval = renewal_interval

        self._lock_key: str = redis_key_builder.lock_job_processing(job_id)
        self._token: str = uuid.uuid4().hex
        self._acquired: bool = False
        self._renewal_greenlet: Optional[gevent.Greenlet] = None
        self._stop_event = gevent.event.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True on success."""
        client = self._redis._get_client()
        full_key = self._redis._build_key(self._lock_key)
        result = client.set(full_key, self._token, nx=True, ex=self._ttl)
        if result:
            self._acquired = True
            self._start_renewal()
            logger.debug(f"Lock acquired: {self._lock_key}")
            return True
        logger.info(
            f"Lock already held for job {self._job_id}, another worker is processing"
        )
        return False

    def release(self) -> bool:
        """Release the lock if we still own it. Returns True if deleted."""
        self._stop_renewal()
        if not self._acquired:
            return False
        try:
            result = self._redis.eval(
                _RELEASE_SCRIPT,
                keys=[self._lock_key],
                args=[self._token],
            )
            released = result == 1
            if released:
                logger.debug(f"Lock released: {self._lock_key}")
            else:
                logger.warning(f"Lock already expired or stolen: {self._lock_key}")
            self._acquired = False
            return released
        except Exception as exc:
            logger.warning(f"Lock release failed for {self._lock_key}: {exc}")
            self._acquired = False
            return False

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "RedisJobLock":
        if not self.acquire():
            raise UnavailableException(
                internal_message=(
                    f"Could not acquire processing lock for job {self._job_id}"
                ),
                retry_after=settings.KB_TASK_RETRY_COUNTDOWN,
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.release()
        return False  # never suppress exceptions

    # ------------------------------------------------------------------
    # Renewal greenlet
    # ------------------------------------------------------------------

    def _start_renewal(self) -> None:
        self._stop_event.clear()
        self._renewal_greenlet = gevent.spawn(self._renewal_loop)

    def _stop_renewal(self) -> None:
        self._stop_event.set()
        g = self._renewal_greenlet
        if g is not None and not g.dead:
            g.kill(block=True, timeout=2)
        self._renewal_greenlet = None

    def _renewal_loop(self) -> None:
        """Periodically reset the lock TTL while we still own it."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._renewal_interval)
            if self._stop_event.is_set():
                break
            try:
                result = self._redis.eval(
                    _RENEW_SCRIPT,
                    keys=[self._lock_key],
                    args=[self._token, self._ttl],
                )
                if result != 1:
                    logger.warning(f"Lock lost during renewal: {self._lock_key}")
                    self._acquired = False
                    break
            except Exception as exc:
                logger.warning(f"Lock renewal failed for {self._lock_key}: {exc}")
