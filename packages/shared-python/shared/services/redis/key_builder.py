"""Helpers for standardized Redis key naming."""

from enum import Enum
from typing import Any, Dict


class RedisKeyType(Enum):
    """Enumeration of Redis key types."""

    USER = "user"
    TASK = "task"
    CONVERSATION = "conversation"
    KNOWLEDGE_BASE = "kb"
    SESSION = "session"
    CACHE = "cache"
    QUEUE = "queue"
    COUNTER = "counter"
    SET = "set"
    HASH = "hash"
    LIST = "list"


class RedisKeyBuilder:
    """Redis key builder."""

    def __init__(self, prefix: str = "knowhere-api"):
        self.prefix = prefix

    def build_key(self, key_type: RedisKeyType, *parts: str, **kwargs) -> str:
        """
        Build a Redis key.

        Args:
            key_type: Key type.
            *parts: Positional key segments.
            **kwargs: Additional named segments.

        Returns:
            Full Redis key.
        """
        key_parts = [self.prefix, key_type.value]
        key_parts.extend(parts)

        # Append any extra key-value segments.
        if kwargs:
            for key, value in sorted(kwargs.items()):
                key_parts.append(f"{key}:{value}")

        return ":".join(key_parts)

    # ==================== User Keys ====================

    def user_config(self, username: str) -> str:
        """User config key."""
        return self.build_key(RedisKeyType.USER, username, "config")

    def user_session(self, username: str) -> str:
        """User session key."""
        return self.build_key(RedisKeyType.USER, username, "session")

    def user_permissions(self, username: str) -> str:
        """User permissions key."""
        return self.build_key(RedisKeyType.USER, username, "permissions")

    def user_activity(self, username: str) -> str:
        """User activity key."""
        return self.build_key(RedisKeyType.USER, username, "activity")

    # ==================== Task Keys ====================

    def task_status(self, task_id: str) -> str:
        """Task status key."""
        return self.build_key(RedisKeyType.TASK, task_id, "status")

    def task_result(self, task_id: str) -> str:
        """Task result key."""
        return self.build_key(RedisKeyType.TASK, task_id, "result")

    def task_metadata(self, task_id: str) -> str:
        """Task metadata key."""
        return self.build_key(RedisKeyType.TASK, task_id, "metadata")

    def task_progress(self, task_id: str) -> str:
        """Task progress key."""
        return self.build_key(RedisKeyType.TASK, task_id, "progress")

    def task_info(self, task_id: str) -> str:
        """Task info key."""
        return self.build_key(RedisKeyType.TASK, task_id, "info")

    def task_queue(self, queue_name: str) -> str:
        """Task queue key."""
        return self.build_key(RedisKeyType.QUEUE, queue_name)

    # ==================== Conversation Keys ====================

    def conversation_state(self, conversation_id: str) -> str:
        """Conversation state key."""
        return self.build_key(RedisKeyType.CONVERSATION, conversation_id, "state")

    def conversation_history(self, conversation_id: str) -> str:
        """Conversation history key."""
        return self.build_key(RedisKeyType.CONVERSATION, conversation_id, "history")

    def conversation_context(self, conversation_id: str) -> str:
        """Conversation context key."""
        return self.build_key(RedisKeyType.CONVERSATION, conversation_id, "context")

    # ==================== Knowledge Base Keys ====================

    def kb_status(self, user_id: str) -> str:
        """Knowledge-base status key."""
        return self.build_key(RedisKeyType.KNOWLEDGE_BASE, user_id, "status")

    def kb_vectors(self, user_id: str) -> str:
        """Knowledge-base vectors key."""
        return self.build_key(RedisKeyType.KNOWLEDGE_BASE, user_id, "vectors")

    def kb_metadata(self, user_id: str) -> str:
        """Knowledge-base metadata key."""
        return self.build_key(RedisKeyType.KNOWLEDGE_BASE, user_id, "metadata")

    def kb_index(self, user_id: str) -> str:
        """Knowledge-base index key."""
        return self.build_key(RedisKeyType.KNOWLEDGE_BASE, user_id, "index")

    # ==================== Session Keys ====================

    def session_data(self, session_id: str) -> str:
        """Session data key."""
        return self.build_key(RedisKeyType.SESSION, session_id, "data")

    def session_expiry(self, session_id: str) -> str:
        """Session expiry key."""
        return self.build_key(RedisKeyType.SESSION, session_id, "expiry")

    # ==================== Cache Keys ====================

    def cache_data(self, cache_key: str) -> str:
        """Cache data key."""
        return self.build_key(RedisKeyType.CACHE, cache_key)

    def cache_metadata(self, cache_key: str) -> str:
        """Cache metadata key."""
        return self.build_key(RedisKeyType.CACHE, cache_key, "metadata")

    # ==================== Counter Keys ====================

    def counter_user_requests(self, username: str) -> str:
        """User-request counter key."""
        return self.build_key(RedisKeyType.COUNTER, "user_requests", username)

    def counter_api_calls(self, api_name: str) -> str:
        """API-call counter key."""
        return self.build_key(RedisKeyType.COUNTER, "api_calls", api_name)

    def counter_task_completed(self, task_type: str) -> str:
        """Task-completed counter key."""
        return self.build_key(RedisKeyType.COUNTER, "task_completed", task_type)

    def rate_limit_api(self, user_id: str, api_name: str) -> str:
        """API rate-limit key."""
        return self.build_key(RedisKeyType.COUNTER, "rate_limit", api_name, user_id)

    # ==================== Set Keys ====================

    def set_active_users(self) -> str:
        """Active-users set key."""
        return self.build_key(RedisKeyType.SET, "active_users")

    def set_online_users(self) -> str:
        """Online-users set key."""
        return self.build_key(RedisKeyType.SET, "online_users")

    def set_processing_tasks(self) -> str:
        """Processing-tasks set key."""
        return self.build_key(RedisKeyType.SET, "processing_tasks")

    # ==================== Hash Keys ====================

    def hash_user_stats(self, username: str) -> str:
        """User-stats hash key."""
        return self.build_key(RedisKeyType.HASH, "user_stats", username)

    def hash_system_stats(self) -> str:
        """System-stats hash key."""
        return self.build_key(RedisKeyType.HASH, "system_stats")

    def hash_task_stats(self, task_id: str) -> str:
        """Task-stats hash key."""
        return self.build_key(RedisKeyType.HASH, "task_stats", task_id)

    # ==================== List Keys ====================

    def list_recent_activities(self, username: str) -> str:
        """Recent-activities list key."""
        return self.build_key(RedisKeyType.LIST, "recent_activities", username)

    def list_error_logs(self) -> str:
        """Error-logs list key."""
        return self.build_key(RedisKeyType.LIST, "error_logs")

    def list_audit_logs(self) -> str:
        """Audit-logs list key."""
        return self.build_key(RedisKeyType.LIST, "audit_logs")

    # ==================== Lock Keys ====================

    def lock_job_processing(self, job_id: str) -> str:
        """Distributed lock key for exclusive job processing."""
        return f"lock:job_processing:{job_id}"

    # ==================== Utility Methods ====================

    def parse_key(self, key: str) -> Dict[str, Any]:
        """
        Parse a Redis key into its components.

        Args:
            key: Redis key.

        Returns:
            Parsed key information.
        """
        if not key.startswith(self.prefix):
            raise ValueError(f"Key '{key}' is not a valid Redis key format")

        parts = key[len(self.prefix) :].lstrip(":").split(":")

        if len(parts) < 2:
            raise ValueError(f"Key '{key}' has an invalid format")

        result = {
            "prefix": self.prefix,
            "type": parts[0],
            "parts": parts[1:],
            "original_key": key,
        }

        return result

    def is_key_type(self, key: str, key_type: RedisKeyType) -> bool:
        """
        Check whether a key belongs to the requested type.

        Args:
            key: Redis key.
            key_type: Key type.

        Returns:
            Whether the key matches the requested type.
        """
        try:
            parsed = self.parse_key(key)
            return parsed["type"] == key_type.value
        except ValueError:
            return False

    def get_key_ttl(self, key_type: RedisKeyType) -> int:
        """
        Get the default TTL for a key type.

        Args:
            key_type: Key type.

        Returns:
            TTL in seconds.
        """
        ttl_mapping = {
            RedisKeyType.USER: 86400,  # 1 day (user_config cache).
            RedisKeyType.TASK: 86400,  # 1 day.
            RedisKeyType.CONVERSATION: 3600 * 2,  # 2 hours.
            RedisKeyType.KNOWLEDGE_BASE: 86400 * 30,  # 30 days.
            RedisKeyType.SESSION: 3600,  # 1 hour.
            RedisKeyType.CACHE: 3600,  # 1 hour.
            RedisKeyType.QUEUE: 86400,  # 1 day.
            RedisKeyType.COUNTER: 86400,  # 1 day.
            RedisKeyType.SET: 86400,  # 1 day.
            RedisKeyType.HASH: 86400,  # 1 day.
            RedisKeyType.LIST: 86400,  # 1 day.
        }

        return ttl_mapping.get(key_type, 3600)  # Default to 1 hour.


# Global key-builder instance.
redis_key_builder = RedisKeyBuilder()
