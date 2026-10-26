"""Celery tasks for Phase B page-memory scope enrichment."""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.core.tasks.base_task import DocumentIngestionBaseTask
from app.services.page_memory.scope_enrichment_service import (
    execute_scope_enrichment,
    execute_structure_enrichment,
)
from shared.core.celery_app import get_celery_app
from shared.core.logging import LogEvent, log_context
from shared.services.jobs.lifecycle.scope_enrichments import (
    sweep_progressive_enrichments_sync,
)
from shared.services.redis.periodic_task_lock import periodic_task_lock

celery_app = get_celery_app()
_SWEEP_PERIOD_SECONDS = 300


class ScopeEnrichmentBaseTask(DocumentIngestionBaseTask):
    """Scope enrichment task base that keeps the parent Job in DONE."""

    finalize_job_on_failure: bool = False


@celery_app.task(
    bind=True,
    base=ScopeEnrichmentBaseTask,
    name="app.core.tasks.enrichment_tasks.enrich_page_memory_scope",
    ignore_result=True,
)
def enrich_page_memory_scope(
    self: Any,
    job_result_id: str,
    scope_id: str,
) -> dict[str, Any]:
    """Run Phase B C1-C7 refinement for one published page-memory scope."""
    with log_context(task_id=self.request.id):
        result = execute_scope_enrichment(
            job_result_id=str(job_result_id),
            scope_id=str(scope_id),
        )
        logger.bind(event=LogEvent.WORKER_TASK_COMPLETE.value).info(
            "Task completed: enrich_page_memory_scope "
            f"job_result_id={job_result_id} scope_id={scope_id} "
            f"status={result.get('status')}"
        )
        return result


@celery_app.task(
    bind=True,
    base=ScopeEnrichmentBaseTask,
    name="app.core.tasks.enrichment_tasks.build_page_memory_structure",
    ignore_result=True,
)
def build_page_memory_structure(
    self: Any,
    job_result_id: str,
) -> dict[str, Any]:
    """Run background M0 -> coarse structure build for a progressive page-memory revision."""
    with log_context(task_id=self.request.id):
        result = execute_structure_enrichment(
            job_result_id=str(job_result_id),
        )
        logger.bind(event=LogEvent.WORKER_TASK_COMPLETE.value).info(
            "Task completed: build_page_memory_structure "
            f"job_result_id={job_result_id} status={result.get('status')}"
        )
        return result


@celery_app.task(
    name="app.core.tasks.enrichment_tasks.sweep_progressive_enrichments",
    ignore_result=True,
)
def sweep_progressive_enrichments() -> dict[str, Any]:
    """Periodic sweep: re-enqueue stuck M0 text revisions and retry failed scopes."""
    with periodic_task_lock(
        "app.core.tasks.enrichment_tasks.sweep_progressive_enrichments",
        period_seconds=_SWEEP_PERIOD_SECONDS,
    ) as acquired:
        if not acquired:
            return {"status": "skipped", "reason": "duplicate Beat firing"}
        return sweep_progressive_enrichments_sync()
