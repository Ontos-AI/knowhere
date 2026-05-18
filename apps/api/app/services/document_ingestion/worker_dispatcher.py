from __future__ import annotations

from celery import signature
from celery.canvas import Signature
from loguru import logger

from shared.core.celery_router import CeleryTaskRouter, task_router
from shared.core.exceptions.domain_exceptions import WorkerHandlingException

_DOCUMENT_PARSE_JOB_TYPE = "document_ingestion"
_DOCUMENT_PARSE_TASK_NAME = "app.core.tasks.document_ingestion_tasks.parse_task"


class DocumentIngestionWorkerDispatcher:
    """Dispatch uploaded Document Ingestion Jobs to worker parsing."""

    def __init__(
        self,
        *,
        celery_task_router: CeleryTaskRouter | None = None,
    ) -> None:
        self._task_router = celery_task_router or task_router

    async def start_uploaded_file_parse(self, *, job_id: str, user_id: str) -> str:
        task_signature = self._build_uploaded_file_parse_signature(
            job_id=job_id,
            user_id=user_id,
        )
        result = task_signature.apply_async()
        if result is None or result.id is None:
            raise WorkerHandlingException(
                internal_message=(
                    "Failed to start Document Ingestion worker parse: "
                    "missing Celery task id"
                )
            )

        task_id = str(result.id)
        signature_options = task_signature.options or {}
        queue_name = signature_options.get("queue")
        logger.info(
            "Document Ingestion worker parse started: "
            f"job_id={job_id}, task_id={task_id}, queue={queue_name}"
        )
        return task_id

    def _build_uploaded_file_parse_signature(
        self,
        *,
        job_id: str,
        user_id: str,
    ) -> Signature:
        queue_name = self._task_router.get_queue_for_job(
            _DOCUMENT_PARSE_JOB_TYPE,
            user_id,
        )
        task_kwargs: dict[str, str] = {
            "user_id": user_id,
            "job_type": _DOCUMENT_PARSE_JOB_TYPE,
        }
        task_signature = signature(
            _DOCUMENT_PARSE_TASK_NAME,
            args=[job_id],
            kwargs=task_kwargs,
        )
        if task_signature is None:
            raise WorkerHandlingException(
                internal_message=(
                    "Failed to build Document Ingestion worker parse: "
                    "missing Celery signature"
                )
            )

        configured_signature = task_signature.set(queue=queue_name)
        if configured_signature is None:
            raise WorkerHandlingException(
                internal_message=(
                    "Failed to configure Document Ingestion worker parse queue"
                )
            )

        return configured_signature
