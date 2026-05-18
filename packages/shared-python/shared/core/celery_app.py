"""
Celery application configuration — Redis-backed broker and result backend.
"""

import os
import socket

from celery import Celery
from kombu import Queue

from shared.core.config import app_config


def get_unique_node_name() -> str:
    """Generate a unique Celery worker node name."""
    hostname = socket.gethostname()
    pid = os.getpid()
    return f"celery@{hostname}-{pid}"


# Create Celery application instance (Redis broker + Redis result backend)
celery_app = Celery(
    "Knowhere API",
    broker=app_config.get_celery_broker_url(),
    backend=app_config.get_celery_result_backend(),
    include=[
        "shared.core.tasks.celery_tasks",
    ],
)

# Task queues — plain Redis transport queues.
# Priority is handled via Celery's Redis transport priority support
# (task_queue_max_priority + task_default_priority).
celery_app.conf.task_queues = (
    # Document Ingestion queues (routed by priority in task invocation)
    Queue("document_ingestion_high", routing_key="document_ingestion.high"),
    Queue("document_ingestion_medium", routing_key="document_ingestion.medium"),
    Queue("document_ingestion_low", routing_key="document_ingestion.low"),
    # Pre-rename ingestion queues kept until old broker messages have drained.
    Queue("kb_high", routing_key="kb.high"),
    Queue("kb_medium", routing_key="kb.medium"),
    Queue("kb_low", routing_key="kb.low"),
    # AI query queue
    Queue("ai_high_priority", routing_key="ai.high"),
    # Default queue for sweeper and other generic tasks
    Queue("default", routing_key="default"),
)

celery_app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Task execution
    task_track_started=True,
    task_time_limit=3600,  # 1 hour hard limit
    task_soft_time_limit=3300,  # 55 minutes soft limit
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    worker_disable_rate_limits=True,
    # Redis serverless does not support the pidbox PSUBSCRIBE control channel.
    worker_enable_remote_control=False,
    task_reject_on_worker_lost=True,
    worker_cancel_long_running_tasks_on_connection_loss=True,
    task_ignore_result=True,
    result_expires=3600,
    # Worker logging
    worker_hijack_root_logger=False,
    worker_log_color=False,
    # Redis broker connection
    broker_connection_retry_on_startup=True,
    broker_connection_retry=True,
    broker_pool_limit=app_config.BROKER_POOL_LIMIT,
    # Redis transport — visibility_timeout must exceed task_time_limit
    broker_transport_options={
        "visibility_timeout": 43200,  # 12 hours
        "retry_on_timeout": True,
        # Keep all Kombu Redis broker keys in one Redis Cluster hash slot.
        "global_keyprefix": "{celery}",
    },
    # Priority support for Redis transport
    task_queue_max_priority=10,
    task_default_priority=5,
    # RedBeat — scheduler backed by the Celery Redis instance
    redbeat_redis_url=app_config.get_celery_redis_url(),
    beat_scheduler="redbeat.RedBeatScheduler",
    redbeat_key_prefix="{redbeat}",  # Hash tag for Redis Cluster CROSSSLOT compatibility
    redbeat_lock_timeout=120,
    beat_max_loop_interval=30,
    # Task routing
    task_routes={
        # Document Ingestion tasks (default medium priority)
        "app.core.tasks.document_ingestion_tasks.*": {
            "queue": "document_ingestion_medium"
        },
        "app.core.tasks.kb_tasks.*": {"queue": "kb_medium"},
        # Webhook orphan recovery
        "app.core.tasks.webhook_tasks.recover_orphaned_webhooks": {"queue": "default"},
        # Sweeper task
        "app.core.tasks.stale_job_sweeper.expire_stale_jobs": {"queue": "default"},
    },
    # Periodic tasks (Celery Beat)
    beat_schedule={
        "recover-orphaned-webhooks": {
            "task": "app.core.tasks.webhook_tasks.recover_orphaned_webhooks",
            "schedule": 1800.0,  # Every 30 minutes
        },
        "expire-stale-jobs": {
            "task": "app.core.tasks.stale_job_sweeper.expire_stale_jobs",
            "schedule": 1800.0,  # Every 30 minutes
        },
    },
)


def get_celery_app() -> Celery:
    """Get the Celery application instance."""
    return celery_app


def get_task_priority(task_type: str) -> int:
    """Get task priority by type."""
    return app_config.get_task_priority(task_type)


def get_queue_name(task_type: str) -> str:
    """Get queue name by task type."""
    return app_config.get_queue_name(task_type)
