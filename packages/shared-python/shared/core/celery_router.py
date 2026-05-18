"""Celery task router with subscription-aware dynamic routing.

This module keeps the priority logic migrated from TaskPriorityService.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict

from loguru import logger

from shared.core.celery_app import get_celery_app

# Note: get_queue_for_job has been simplified and no longer needs direct DB access.

_DOCUMENT_INGESTION_TASK_TYPE = "document_ingestion"
_LEGACY_DOCUMENT_INGESTION_TASK_TYPES = frozenset(
    {
        "kb_encoding",
        "kb_management",
    }
)


def _normalize_task_type(task_type: str) -> str:
    if task_type in _LEGACY_DOCUMENT_INGESTION_TASK_TYPES:
        return _DOCUMENT_INGESTION_TASK_TYPE
    return task_type


class TaskType(Enum):
    """Task-type enum."""

    AI_QUERY = "ai_query"
    USER_AUTH = "user_auth"
    URGENT_DOCUMENT = "urgent_document"
    DOCUMENT_PROCESSING = "document_processing"
    DOCUMENT_INGESTION = "document_ingestion"
    BATCH_PROCESSING = "batch_processing"
    ANALYTICS = "analytics"
    BACKUP = "backup"
    LOG_PROCESSING = "log_processing"


class UserLevel(Enum):
    """User-level enum."""

    VIP = "vip"
    PREMIUM = "premium"
    STANDARD = "standard"
    BASIC = "basic"


class DocumentImportance(Enum):
    """Document-importance enum."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class TaskContext:
    """Task execution context."""

    task_type: TaskType
    user_id: str
    user_level: UserLevel = UserLevel.STANDARD
    document_importance: DocumentImportance = DocumentImportance.MEDIUM
    is_urgent: bool = False
    resource_requirements: str = "medium"
    estimated_duration: int = 300  # Seconds.
    retry_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


class CeleryTaskRouter:
    """Celery task router."""

    def __init__(self):
        self.celery_app = get_celery_app()
        # Note: get_queue_for_job no longer needs subscription_repo.

        # Base priority config migrated from TaskPriorityService.
        self.base_priorities = {
            TaskType.AI_QUERY: 10,
            TaskType.USER_AUTH: 10,
            TaskType.URGENT_DOCUMENT: 10,
            TaskType.DOCUMENT_PROCESSING: 5,
            TaskType.DOCUMENT_INGESTION: 6,
            TaskType.BATCH_PROCESSING: 5,
            TaskType.ANALYTICS: 1,
            TaskType.BACKUP: 1,
            TaskType.LOG_PROCESSING: 1,
        }

        # User-level weights.
        self.user_level_weights = {
            UserLevel.VIP: 1.2,
            UserLevel.PREMIUM: 1.1,
            UserLevel.STANDARD: 1.0,
            UserLevel.BASIC: 0.9,
        }

        # Document-importance weights.
        self.document_importance_weights = {
            DocumentImportance.CRITICAL: 1.3,
            DocumentImportance.HIGH: 1.1,
            DocumentImportance.MEDIUM: 1.0,
            DocumentImportance.LOW: 0.8,
        }

        # Urgent-task weight.
        self.urgent_weight = 1.5

        # Retry penalty weight.
        self.retry_penalty = 0.1

    def calculate_priority(self, context: TaskContext) -> int:
        """
        Calculate task priority using the migrated TaskPriorityService logic.

        Args:
            context: Task context.

        Returns:
            Calculated priority in the range 1-10.
        """
        try:
            # Get the base priority.
            base_priority = self.base_priorities.get(context.task_type, 5)

            # Apply the user-level weight.
            user_weight = self.user_level_weights.get(context.user_level, 1.0)

            # Apply the document-importance weight.
            doc_weight = self.document_importance_weights.get(
                context.document_importance, 1.0
            )

            # Apply the urgent-task weight.
            urgent_weight = self.urgent_weight if context.is_urgent else 1.0

            # Apply the retry penalty.
            retry_penalty = 1.0 - (context.retry_count * self.retry_penalty)
            retry_penalty = max(0.1, retry_penalty)  # Minimum weight is 0.1.

            # Compute the final priority.
            final_priority = (
                base_priority * user_weight * doc_weight * urgent_weight * retry_penalty
            )

            # Clamp the result to the 1-10 range.
            final_priority = max(1, min(10, int(final_priority)))

            logger.debug(
                f"Task priority calculation: base={base_priority}, "
                f"user_weight={user_weight}, doc_weight={doc_weight}, "
                f"urgent_weight={urgent_weight}, retry_penalty={retry_penalty}, "
                f"final={final_priority}"
            )

            return final_priority

        except Exception as e:
            logger.error(f"Failed to compute task priority: {e}")
            return 5  # Default medium priority.

    def create_task_context(
        self, task_type: str, user_id: str, **kwargs
    ) -> TaskContext:
        """
        Create task context from the migrated TaskPriorityService inputs.

        Args:
            task_type: Task type.
            user_id: User ID.
            **kwargs: Additional context values.

        Returns:
            Task context.
        """
        try:
            # Parse the task type.
            task_type_enum = TaskType(_normalize_task_type(task_type))

            # Parse the user level.
            user_level = UserLevel(kwargs.get("user_level", "standard"))

            # Parse the document importance.
            document_importance = DocumentImportance(
                kwargs.get("document_importance", "medium")
            )

            return TaskContext(
                task_type=task_type_enum,
                user_id=user_id,
                user_level=user_level,
                document_importance=document_importance,
                is_urgent=kwargs.get("is_urgent", False),
                resource_requirements=kwargs.get("resource_requirements", "medium"),
                estimated_duration=kwargs.get("estimated_duration", 300),
                retry_count=kwargs.get("retry_count", 0),
                metadata=kwargs.get("metadata", {}),
            )
        except Exception as e:
            logger.error(f"Failed to create task context: {e}")
            # Return a safe default context.
            return TaskContext(
                task_type=TaskType.DOCUMENT_PROCESSING,
                user_id=user_id,
                metadata=kwargs.get("metadata", {}),
            )

    def get_queue_for_job(self, job_type: str, user_id: str) -> str:
        """
        Resolve the queue name for a job based on job type and subscription.

        Args:
            job_type: Task type such as document_ingestion or ai_query.
            user_id: User ID.

        Returns:
            Queue name.
        """
        try:
            # TODO: temporary simplified path to avoid async work here.
            if job_type in _LEGACY_DOCUMENT_INGESTION_TASK_TYPES:
                return "kb_low"
            if job_type == _DOCUMENT_INGESTION_TASK_TYPE:
                return "document_ingestion_low"
            elif job_type in ["ai_query", "user_auth", "urgent_document"]:
                return "ai_high_priority"
            elif job_type in ["document_processing"]:
                return "document_processing"
            elif job_type in ["batch_processing"]:
                return "batch_processing"
            elif job_type in ["analytics"]:
                return "analytics_queue"
            elif job_type in ["backup"]:
                return "backup_queue"
            elif job_type in ["log_processing"]:
                return "log_processing"
            else:
                # Default to the medium-priority queue.
                return f"{job_type}_medium"

        except Exception as e:
            logger.error(f"Failed to get queue for user {user_id}: {e}")
            # Default to a medium-priority queue on failure.
            if job_type in _LEGACY_DOCUMENT_INGESTION_TASK_TYPES:
                return "kb_medium"
            if job_type == _DOCUMENT_INGESTION_TASK_TYPE:
                return "document_ingestion_medium"
            elif job_type in ["ai_query", "user_auth", "urgent_document"]:
                return "ai_high_priority"
            else:
                return "default"

    def route_task(self, name, args, kwargs, options, task=None, **kwds):
        """
        Route a Celery task dynamically.

        Celery calls this hook to determine task routing based on task type and
        user-priority signals.
        """
        try:
            # Extract job_type and user_id from kwargs.
            job_type = kwargs.get("job_type")
            user_id = kwargs.get("user_id")

            # Fall back to positional args for upload_url_file_task-style calls.
            if not user_id and len(args) >= 3:
                user_id = args[2]  # The third argument is user_id.

            if job_type and user_id:
                # Build context and compute the priority.
                context = self.create_task_context(
                    task_type=job_type,
                    user_id=user_id,
                    user_level=kwargs.get("user_level", "standard"),
                    document_importance=kwargs.get("document_importance", "medium"),
                    is_urgent=kwargs.get("is_urgent", False),
                    retry_count=kwargs.get("retry_count", 0),
                    metadata=kwargs.get("metadata", {}),
                )

                # Resolve the queue name.
                queue_name = self.get_queue_for_job(job_type, user_id)

                # Compute the final priority.
                priority = self.calculate_priority(context)

                logger.info(
                    f"Routed task {name} to queue {queue_name} "
                    f"(user: {user_id}, type: {job_type}, priority: {priority})"
                )

                return {"queue": queue_name, "priority": priority}
            else:
                # Use the default Celery routing behavior.
                return None

        except Exception as e:
            logger.error(f"Task routing failed: {e}")
            return None


# Create router instance
task_router = CeleryTaskRouter()

# Register router with Celery - combine function routing and static routing
celery_app = get_celery_app()

# Save static routes configured in celery_app.py
static_routes = (
    celery_app.conf.task_routes.copy()
    if isinstance(celery_app.conf.task_routes, dict)
    else {}
)

# Celery supports router list: try function router first, fallback to static dict
# 1. task_router.route_task: Dynamic routing for Document Ingestion tasks.
# 2. static_routes: Static routing (webhook and other fixed routes, from celery_app.py)
celery_app.conf.task_routes = [
    task_router.route_task,  # Dynamic routing priority
    static_routes,  # Fallback to static configuration
]
