"""
Celery configuration — Redis-backed broker and result backend.
"""

from typing import Dict

from pydantic import BaseModel, Field


class CeleryConfig(BaseModel):
    """Celery configuration backed by a dedicated Redis instance."""

    # Dedicated Redis instance for Celery broker / result backend / RedBeat.
    # Separate from the application Redis (REDIS_*) to isolate connection pools.
    CELERY_REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="Full Celery Redis URL for broker / result backend / RedBeat",
    )

    # Broker connection pool
    BROKER_POOL_LIMIT: int = Field(
        default=10, description="Celery broker connection pool limit"
    )

    # Task retry configuration
    DOCUMENT_INGESTION_TASK_MAX_RETRIES: int = Field(
        default=2,
        description="Document Ingestion task max retries",
    )
    DOCUMENT_INGESTION_TASK_RETRY_COUNTDOWN: int = Field(
        default=120,
        description="Document Ingestion task retry countdown (seconds)",
    )
    PYMUPDF_MAX_CONCURRENT: int = Field(
        default=2,
        ge=1,
        description="Per-pod PyMuPDF child-process concurrency cap",
    )

    # Task priority configuration
    TASK_PRIORITIES: Dict[str, int] = Field(
        default={
            "ai_query": 10,
            "user_auth": 10,
            "urgent_document": 10,
            "document_processing": 5,
            "document_ingestion": 6,
            "batch_processing": 5,
            "analytics": 1,
            "backup": 1,
            "log_processing": 1,
        },
        description="Task priority mapping",
    )

    # Queue routing configuration
    QUEUE_MAPPING: Dict[str, str] = Field(
        default={
            "ai_query": "ai_high_priority",
            "user_auth": "auth_queue",
            "urgent_document": "document_urgent",
            "document_processing": "document_processing",
            "document_ingestion": "document_ingestion_low",
            "batch_processing": "batch_processing",
            "analytics": "analytics_queue",
            "backup": "backup_queue",
            "log_processing": "log_processing",
        },
        description="Queue routing mapping",
    )

    def get_task_priority(self, task_type: str) -> int:
        """Get task priority by type."""
        return self.TASK_PRIORITIES.get(task_type, 5)

    def get_queue_name(self, task_type: str) -> str:
        """Get queue name by task type."""
        return self.QUEUE_MAPPING.get(task_type, "default")

    def get_celery_redis_url(self) -> str:
        """Build the Redis URL for Celery broker, result backend, and RedBeat."""
        return self.CELERY_REDIS_URL

    def get_celery_broker_url(self) -> str:
        """Get Celery broker URL (Redis)."""
        return self.get_celery_redis_url()

    def get_celery_result_backend(self) -> str:
        """Get Celery result backend URL (Redis)."""
        return self.get_celery_redis_url()
