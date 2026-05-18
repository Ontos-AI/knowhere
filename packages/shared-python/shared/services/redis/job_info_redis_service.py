"""
Redis service for job summary information.
Maintains shared job-info cache entries for both API and worker services.
"""

from typing import Any, Dict, Optional

from loguru import logger

from shared.services.redis.redis_service import RedisService
from shared.utils.redis_key_builder import RedisKeyType, redis_key_builder


class JobInfoRedisService:
    """Redis service for cached job information."""

    # Cache TTL aligned with the Job lifecycle TTL.
    JOB_INFO_TTL = redis_key_builder.get_key_ttl(RedisKeyType.TASK)

    def __init__(self, redis_service: RedisService):
        self.redis = redis_service

    async def save_job_info(self, job_id: str, job_info: Dict[str, Any]) -> bool:
        """
        Save job information to Redis with the Job-aligned TTL.

        Args:
            job_id: Job ID.
            job_info: Job info payload including IDs, ownership, and source data.

        Returns:
            Whether the save succeeded.
        """
        try:
            key = redis_key_builder.task_info(job_id)
            await self.redis.set(key, job_info, ttl=self.JOB_INFO_TTL)
            logger.debug(
                f"Job info saved to Redis: job_id={job_id}, ttl={self.JOB_INFO_TTL}s"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to save job info to Redis: {e}")
            return False

    async def get_job_info(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Load job information from Redis.

        Args:
            job_id: Job ID.

        Returns:
            Job info payload, or None when missing.
        """
        try:
            key = redis_key_builder.task_info(job_id)
            job_info = await self.redis.get(key)
            if job_info:
                logger.debug(f"Loaded job info from Redis: job_id={job_id}")
            return job_info
        except Exception as e:
            logger.error(f"Failed to load job info from Redis: {e}")
            return None

    async def update_job_info(self, job_id: str, updates: Dict[str, Any]) -> bool:
        """
        Update cached job information and refresh its TTL.

        Args:
            job_id: Job ID.
            updates: Fields to merge into the cached record.

        Returns:
            Whether the update succeeded.
        """
        try:
            job_info = await self.get_job_info(job_id)
            if job_info:
                job_info.update(updates)
                return await self.save_job_info(job_id, job_info)
            return False
        except Exception as e:
            logger.error(f"Failed to update job info: {e}")
            return False

    async def delete_job_info(self, job_id: str) -> bool:
        """
        Delete cached job information from Redis.

        Args:
            job_id: Job ID.

        Returns:
            Whether the delete succeeded.
        """
        try:
            key = redis_key_builder.task_info(job_id)
            await self.redis.delete(key)
            logger.debug(f"Job info deleted from Redis: job_id={job_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete job info: {e}")
            return False
