"""Acknowledge obsolete Beat deliveries without restarting primary indexing."""

from shared.core.celery_app import celery_app


@celery_app.task(name="shared.services.retrieval.lexical.reconcile.reconcileIndexing")
def reconcileIndexing() -> int:
    # Existing 30-minute stale-job sweeping owns failure settlement. A delayed
    # message from the superseded schedule cannot authorize another execution.
    return 0
