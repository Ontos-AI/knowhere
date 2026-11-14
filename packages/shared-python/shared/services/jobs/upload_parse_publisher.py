from loguru import logger

from shared.core.celery_app import get_celery_app
from shared.core.celery_router import CeleryTaskRouter, task_router
from shared.core.exceptions.domain_exceptions import WorkerHandlingException

_DOCUMENT_PARSE_JOB_TYPE = "document_ingestion"
_DOCUMENT_PARSE_TASK_NAME = "app.core.tasks.document_ingestion_tasks.parse_task"


def publish_upload_parse(
    *,
    job_id: str,
    user_id: str,
    celery_task_router: CeleryTaskRouter = task_router,
) -> str:
    queue_name = celery_task_router.get_queue_for_job(_DOCUMENT_PARSE_JOB_TYPE, user_id)
    task_signature = get_celery_app().signature(
        _DOCUMENT_PARSE_TASK_NAME,
        args=[job_id],
        kwargs={"user_id": user_id, "job_type": _DOCUMENT_PARSE_JOB_TYPE},
        queue=queue_name,
    )
    result = task_signature.apply_async()
    if result is None or result.id is None:
        raise WorkerHandlingException(
            internal_message="Document Ingestion parse publication returned no Celery task id"
        )

    task_id = str(result.id)
    logger.info(
        "Document Ingestion parse published: "
        f"job_id={job_id}, task_id={task_id}, queue={queue_name}"
    )
    return task_id
