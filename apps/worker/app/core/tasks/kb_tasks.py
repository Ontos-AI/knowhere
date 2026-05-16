"""
Knowledge Base Management Celery Tasks

Sync implementation for gevent worker pool.
All I/O operations use sync services that yield cooperatively under gevent.
"""

# Base task class
from app.core.tasks.base_task import KBBaseTask
from app.services.document_ingestion.service import parse_uploaded_file_job
from app.services.workload.url_upload_service import upload_url_file
from loguru import logger

from shared.core.celery_app import get_celery_app
from shared.core.config import settings
from shared.core.exceptions import RETRYABLE_EXCEPTIONS

# Exception handling
from shared.core.exceptions.domain_exceptions import (
    WorkerHandlingException,
)
from shared.core.logging import LogEvent, log_context

# Get Celery application
celery_app = get_celery_app()



@celery_app.task(
    bind=True,
    base=KBBaseTask,
    name="app.core.tasks.kb_tasks.upload_url_file_task",
    ignore_result=True,
    autoretry_for=RETRYABLE_EXCEPTIONS,
    retry_kwargs={
        "countdown": settings.KB_TASK_RETRY_COUNTDOWN,
        "max_retries": settings.KB_TASK_MAX_RETRIES,
    },
)
def upload_url_file_task(
    self,
    job_id: str,
    source_url: str,
    user_id: str | None = None,
    job_type: str | None = None,
):
    """Download file from URL and upload to S3."""
    with log_context(task_id=self.request.id):
        if not job_id:
            raise WorkerHandlingException(
                user_message="An unexpected system error occurred",
                internal_message="Worker task 'upload_url_file_task' called without job_id",
            )

        result = _upload_url_file(job_id, source_url, user_id, job_type)

        logger.bind(event=LogEvent.WORKER_TASK_COMPLETE.value).info(
            "Task completed: upload_url_file_task"
        )
        return result


def _upload_url_file(
    job_id: str, source_url: str, user_id: str | None, job_type: str | None = None
):
    """Sync URL file download and upload to S3."""
    return upload_url_file(job_id, source_url, user_id, job_type)


@celery_app.task(
    bind=True,
    base=KBBaseTask,
    name="app.core.tasks.kb_tasks.parse_task",
    ignore_result=True,
    autoretry_for=RETRYABLE_EXCEPTIONS,
    retry_kwargs={
        "countdown": settings.KB_TASK_RETRY_COUNTDOWN,
        "max_retries": settings.KB_TASK_MAX_RETRIES,
    },
)
def parse_task(
    self, job_id: str, user_id: str | None = None, job_type: str = "kb_management"
):
    """Parse and vectorize task (file already uploaded to S3)."""
    with log_context(task_id=self.request.id):
        if not job_id:
            raise WorkerHandlingException(
                user_message="An unexpected system error occurred",
                internal_message="Worker task 'parse_task' called without job_id",
            )

        result = _parse(job_id, user_id)

        logger.bind(event=LogEvent.WORKER_TASK_COMPLETE.value).info(
            "Task completed: parse_task"
        )
        return result


def _parse(job_id: str, user_id: str | None):
    """Sync parse and vectorize (file already uploaded to S3)."""
    return parse_uploaded_file_job(job_id, user_id)
