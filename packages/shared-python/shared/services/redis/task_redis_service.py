"""Redis service for task state and results."""

from typing import Any, Dict, List, Optional

from loguru import logger

from shared.services.redis.redis_service import RedisService
from shared.utils.redis_key_builder import RedisKeyType, redis_key_builder


class TaskRedisService:
    """Redis service for task-related data."""

    def __init__(self, redis_service: RedisService):
        self.redis = redis_service

    async def create_task(self, task_id: str, task_data: Dict[str, Any]) -> bool:
        """Create a task record."""
        try:
            # Save task metadata.
            metadata_key = redis_key_builder.task_metadata(task_id)
            await self.redis.hset(metadata_key, mapping=task_data)
            await self.redis.expire(
                metadata_key, redis_key_builder.get_key_ttl(RedisKeyType.TASK)
            )

            # Set the initial status.
            await self.set_task_status(task_id, "pending")

            # Add the task to the processing set.
            processing_tasks_key = redis_key_builder.set_processing_tasks()
            await self.redis.sadd(processing_tasks_key, task_id)
            await self.redis.expire(
                processing_tasks_key, redis_key_builder.get_key_ttl(RedisKeyType.SET)
            )

            logger.info(f"Task {task_id} created successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to create task {task_id}: {e}")
            return False

    async def set_task_status(self, task_id: str, status: str) -> bool:
        """Set the task status."""
        try:
            status_key = redis_key_builder.task_status(task_id)
            await self.redis.set(
                status_key, status, ttl=redis_key_builder.get_key_ttl(RedisKeyType.TASK)
            )

            # Refresh the task progress payload.
            progress_key = redis_key_builder.task_progress(task_id)
            progress_data = {
                "status": status,
                "timestamp": self._get_current_timestamp(),
            }
            await self.redis.hset(progress_key, mapping=progress_data)
            await self.redis.expire(
                progress_key, redis_key_builder.get_key_ttl(RedisKeyType.TASK)
            )

            logger.debug(f"Task {task_id} status updated to: {status}")
            return True
        except Exception as e:
            logger.error(f"Failed to set task {task_id} status: {e}")
            return False

    async def get_task_status(self, task_id: str) -> str:
        """Get the task status."""
        try:
            status_key = redis_key_builder.task_status(task_id)
            status = await self.redis.get(status_key, "unknown")
            return status
        except Exception as e:
            logger.error(f"Failed to get task {task_id} status: {e}")
            return "unknown"

    async def save_task_result(self, task_id: str, result: Dict[str, Any]) -> bool:
        """Save the task result."""
        try:
            result_key = redis_key_builder.task_result(task_id)
            await self.redis.set(
                result_key, result, ttl=redis_key_builder.get_key_ttl(RedisKeyType.TASK)
            )

            # Mark the task as done.
            await self.set_task_status(task_id, "done")

            # Remove the task from the processing set.
            processing_tasks_key = redis_key_builder.set_processing_tasks()
            await self.redis.srem(processing_tasks_key, task_id)

            logger.info(f"Result for task {task_id} saved successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to save result for task {task_id}: {e}")
            return False

    async def get_task_result(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get the task result."""
        try:
            result_key = redis_key_builder.task_result(task_id)
            result = await self.redis.get(result_key)
            return result
        except Exception as e:
            logger.error(f"Failed to get result for task {task_id}: {e}")
            return None

    async def update_task_progress(
        self, task_id: str, progress: int, message: str = ""
    ) -> bool:
        """Update task progress."""
        try:
            progress_key = redis_key_builder.task_progress(task_id)
            progress_data = {
                "progress": progress,
                "message": message,
                "timestamp": self._get_current_timestamp(),
            }
            await self.redis.hset(progress_key, mapping=progress_data)
            await self.redis.expire(
                progress_key, redis_key_builder.get_key_ttl(RedisKeyType.TASK)
            )
            return True
        except Exception as e:
            logger.error(f"Failed to update progress for task {task_id}: {e}")
            return False

    async def get_task_progress(self, task_id: str) -> Dict[str, Any]:
        """Get task progress."""
        try:
            progress_key = redis_key_builder.task_progress(task_id)
            progress = await self.redis.hgetall(progress_key)
            return progress
        except Exception as e:
            logger.error(f"Failed to get progress for task {task_id}: {e}")
            return {}

    async def mark_task_failed(self, task_id: str, error_message: str) -> bool:
        """Mark a task as failed."""
        try:
            # Update the task status.
            await self.set_task_status(task_id, f"failed: {error_message}")

            # Remove the task from the processing set.
            processing_tasks_key = redis_key_builder.set_processing_tasks()
            await self.redis.srem(processing_tasks_key, task_id)

            # Append an error log entry.
            error_logs_key = redis_key_builder.list_error_logs()
            error_data = {
                "task_id": task_id,
                "error": error_message,
                "timestamp": self._get_current_timestamp(),
            }
            await self.redis.rpush(error_logs_key, error_data)
            await self.redis.expire(
                error_logs_key, redis_key_builder.get_key_ttl(RedisKeyType.LIST)
            )

            logger.error(f"Task {task_id} marked as failed: {error_message}")
            return True
        except Exception as e:
            logger.error(f"Error while marking task {task_id} as failed: {e}")
            return False

    async def get_processing_tasks(self) -> List[str]:
        """Get the list of processing tasks."""
        try:
            processing_tasks_key = redis_key_builder.set_processing_tasks()
            tasks = await self.redis.smembers(processing_tasks_key)
            return list(tasks)
        except Exception as e:
            logger.error(f"Failed to get the in-progress task list: {e}")
            return []

    async def cleanup_task(self, task_id: str) -> bool:
        """Clean up all task-related data."""
        try:
            # Delete all related keys.
            keys_to_delete = [
                redis_key_builder.task_status(task_id),
                redis_key_builder.task_result(task_id),
                redis_key_builder.task_metadata(task_id),
                redis_key_builder.task_progress(task_id),
            ]

            await self.redis.delete(*keys_to_delete)

            # Remove the task from the processing set.
            processing_tasks_key = redis_key_builder.set_processing_tasks()
            await self.redis.srem(processing_tasks_key, task_id)

            logger.info(f"Task {task_id} data cleanup completed")
            return True
        except Exception as e:
            logger.error(f"Failed to clean up data for task {task_id}: {e}")
            return False

    def _get_current_timestamp(self) -> str:
        """Get the current timestamp."""
        import time

        return str(int(time.time()))
