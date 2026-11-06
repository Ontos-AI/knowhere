"""Indexing tasks share the worker deployment, without parser failure hooks."""

from shared.core.celery_app import celery_app
from loguru import logger


@celery_app.task(name="shared.services.retrieval.lexical.tasks.runIndexing")
def runIndexing(jobId: str) -> str:
    # Old queue deliveries may survive deployment. Acknowledge them without
    # consuming a new execution or acting for a later manual HTTP caller.
    logger.warning("Ignored obsolete indexing delivery: job_id={}", jobId)
    return "skipped"
