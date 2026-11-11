import asyncio

from shared.core.celery_router import CeleryTaskRouter, task_router
from shared.services.jobs.upload_parse_publisher import publish_upload_parse


class DocumentIngestionWorkerDispatcher:
    """Dispatch uploaded Document Ingestion Jobs to worker parsing."""

    def __init__(
        self,
        *,
        celery_task_router: CeleryTaskRouter | None = None,
    ) -> None:
        self._task_router = celery_task_router or task_router

    async def start_uploaded_file_parse(self, *, job_id: str, user_id: str) -> str:
        return await asyncio.to_thread(
            publish_upload_parse,
            job_id=job_id,
            user_id=user_id,
            celery_task_router=self._task_router,
        )
