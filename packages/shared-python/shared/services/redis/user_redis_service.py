"""Redis service for user-related state."""

from typing import Any, Dict, List, Optional

from loguru import logger

from shared.services.redis.redis_service import RedisService
from shared.utils.redis_key_builder import RedisKeyType, redis_key_builder


class UserRedisService:
    """Redis service for user data."""

    def __init__(self, redis_service: RedisService):
        self.redis = redis_service

    # save_user_config and get_user_config were removed; user config is no longer stored in Redis.

    async def update_user_session(
        self, user_id: str, session_data: Dict[str, Any]
    ) -> bool:
        """Update a user session."""
        try:
            session_key = redis_key_builder.user_session(user_id)
            await self.redis.set(
                session_key,
                session_data,
                ttl=redis_key_builder.get_key_ttl(RedisKeyType.SESSION),
            )

            # Add the user to the online-users set.
            online_users_key = redis_key_builder.set_online_users()
            await self.redis.sadd(online_users_key, user_id)
            await self.redis.expire(
                online_users_key, redis_key_builder.get_key_ttl(RedisKeyType.SET)
            )

            logger.debug(f"Updated session for user {user_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to update session for user {user_id}: {e}")
            return False

    async def get_user_session(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get a user session."""
        try:
            session_key = redis_key_builder.user_session(user_id)
            session = await self.redis.get(session_key)
            return session
        except Exception as e:
            logger.error(f"Failed to get session for user {user_id}: {e}")
            return None

    async def update_user_activity(
        self, user_id: str, activity: str = "active"
    ) -> bool:
        """Update user activity."""
        try:
            activity_key = redis_key_builder.user_activity(user_id)
            activity_data = {
                "activity": activity,
                "timestamp": self._get_current_timestamp(),
                "user_id": user_id,
            }
            await self.redis.hset(activity_key, mapping=activity_data)
            await self.redis.expire(
                activity_key, redis_key_builder.get_key_ttl(RedisKeyType.USER)
            )

            # Add the user to the active-users set.
            active_users_key = redis_key_builder.set_active_users()
            await self.redis.sadd(active_users_key, user_id)
            await self.redis.expire(
                active_users_key, redis_key_builder.get_key_ttl(RedisKeyType.SET)
            )

            logger.debug(f"Updated activity for user {user_id}: {activity}")
            return True
        except Exception as e:
            logger.error(f"Failed to update activity for user {user_id}: {e}")
            return False

    async def get_user_activity(self, user_id: str) -> Dict[str, Any]:
        """Get user activity."""
        try:
            activity_key = redis_key_builder.user_activity(user_id)
            activity = await self.redis.hgetall(activity_key)
            return activity
        except Exception as e:
            logger.error(f"Failed to get activity for user {user_id}: {e}")
            return {}

    async def set_user_permissions(self, user_id: str, permissions: List[str]) -> bool:
        """Set user permissions."""
        try:
            permissions_key = redis_key_builder.user_permissions(user_id)
            permissions_data = {
                "permissions": permissions,
                "timestamp": self._get_current_timestamp(),
            }
            await self.redis.set(
                permissions_key,
                permissions_data,
                ttl=redis_key_builder.get_key_ttl(RedisKeyType.USER),
            )

            logger.info(f"Permissions set successfully for user {user_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to set permissions for user {user_id}: {e}")
            return False

    async def get_user_permissions(self, user_id: str) -> List[str]:
        """Get user permissions."""
        try:
            permissions_key = redis_key_builder.user_permissions(user_id)
            permissions_data = await self.redis.get(permissions_key)

            if permissions_data and isinstance(permissions_data, dict):
                return permissions_data.get("permissions", [])
            return []
        except Exception as e:
            logger.error(f"Failed to get permissions for user {user_id}: {e}")
            return []

    async def increment_user_requests(self, user_id: str) -> int:
        """Increment the user request counter."""
        try:
            counter_key = redis_key_builder.counter_user_requests(user_id)
            count = await self.redis.incr(counter_key)
            await self.redis.expire(
                counter_key, redis_key_builder.get_key_ttl(RedisKeyType.COUNTER)
            )
            return count
        except Exception as e:
            logger.error(f"Failed to increment request count for user {user_id}: {e}")
            return 0

    async def get_user_requests_count(self, user_id: str) -> int:
        """Get the user request count."""
        try:
            counter_key = redis_key_builder.counter_user_requests(user_id)
            count = await self.redis.get(counter_key, 0)
            return int(count)
        except Exception as e:
            logger.error(f"Failed to get request count for user {user_id}: {e}")
            return 0

    async def get_online_users(self) -> List[str]:
        """Get online users."""
        try:
            online_users_key = redis_key_builder.set_online_users()
            users = await self.redis.smembers(online_users_key)
            return list(users)
        except Exception as e:
            logger.error(f"Failed to get online user list: {e}")
            return []

    async def get_active_users(self) -> List[str]:
        """Get active users."""
        try:
            active_users_key = redis_key_builder.set_active_users()
            users = await self.redis.smembers(active_users_key)
            return list(users)
        except Exception as e:
            logger.error(f"Failed to get active user list: {e}")
            return []

    async def user_logout(self, user_id: str) -> bool:
        """Log a user out."""
        try:
            # Remove the user from the online-users set.
            online_users_key = redis_key_builder.set_online_users()
            await self.redis.srem(online_users_key, user_id)

            # Delete the session payload.
            session_key = redis_key_builder.user_session(user_id)
            await self.redis.delete(session_key)

            # Update the activity status.
            await self.update_user_activity(user_id, "logout")

            logger.info(f"User {user_id} logged out successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to log out user {user_id}: {e}")
            return False

    async def cleanup_user_data(self, user_id: str) -> bool:
        """Clean up user-related data."""
        try:
            # Delete all related keys.
            keys_to_delete = [
                redis_key_builder.user_config(user_id),
                redis_key_builder.user_session(user_id),
                redis_key_builder.user_activity(user_id),
                redis_key_builder.user_permissions(user_id),
                redis_key_builder.counter_user_requests(user_id),
            ]

            await self.redis.delete(*keys_to_delete)

            # Remove the user from shared sets.
            online_users_key = redis_key_builder.set_online_users()
            active_users_key = redis_key_builder.set_active_users()
            await self.redis.srem(online_users_key, user_id)
            await self.redis.srem(active_users_key, user_id)

            logger.info(f"Completed data cleanup for user {user_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to clean up data for user {user_id}: {e}")
            return False

    def _get_current_timestamp(self) -> str:
        """Get the current timestamp."""
        import time

        return str(int(time.time()))
