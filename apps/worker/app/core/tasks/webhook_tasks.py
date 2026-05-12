"""
Webhook-related Celery tasks.

Only the orphan recovery beat task remains here. Webhook delivery is always
published through QStash, which owns retries and callback handling.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from loguru import logger

from shared.core.celery_app import get_celery_app
from shared.core.logging import (
    LogEvent,
)
from shared.services.redis.periodic_task_lock import periodic_task_lock

# Matches the beat_schedule period in celery_app.py
_WEBHOOK_RECOVERY_PERIOD_SECONDS = 1800
_WEBHOOK_CALLBACK_TEXT_LIMIT = 4096

celery_app = get_celery_app()


def _build_reconciliation_log_idempotency_key(
    qstash_message_id: str,
    event_id: str,
    status: str,
) -> str:
    """Build a fixed-width idempotency key for reconstructed QStash logs."""
    return str(uuid5(NAMESPACE_URL, f"{qstash_message_id}:{event_id}:{status}"))


def _truncate_callback_text(value: str | None) -> str | None:
    """Trim QStash log text to the database column limit used by callbacks."""
    if not value:
        return None

    return value[:_WEBHOOK_CALLBACK_TEXT_LIMIT]


def _reconcile_stale_delivering_events(
    db: Any,
    publisher: Any,
    cutoff_time: datetime,
) -> int:
    """Reconcile stale delivering events whose QStash message is terminal."""
    from sqlalchemy import select as sa_select

    from shared.models.database.webhook import WebhookEvent, WebhookEventStatus
    from shared.models.database.webhook_log import WebhookLog

    result = db.execute(
        sa_select(WebhookEvent)
        .where(
            WebhookEvent.status == WebhookEventStatus.DELIVERING,
            WebhookEvent.qstash_message_id.is_not(None),
            WebhookEvent.updated_at < cutoff_time,
        )
        .limit(100)
    )
    stale_events = result.scalars().all()
    reconciled = 0

    for event in stale_events:
        qstash_message_id = str(event.qstash_message_id)
        delivery_status = publisher.get_terminal_delivery_status(qstash_message_id)
        if delivery_status is None:
            continue

        event.status = delivery_status.status
        event.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

        db.add(
            WebhookLog(
                job_id=event.job_id,
                event_id=event.id,
                webhook_url=event.target_url,
                attempt_number=max(event.attempts, 1),
                request_payload=event.payload,
                signature="",
                idempotency_key=_build_reconciliation_log_idempotency_key(
                    qstash_message_id,
                    event.id,
                    delivery_status.status,
                ),
                response_status_code=delivery_status.response_status_code,
                response_body=_truncate_callback_text(delivery_status.response_body),
                error_message=_truncate_callback_text(delivery_status.error_message),
                duration_ms=0,
                delivery_provider="qstash",
                qstash_message_id=qstash_message_id,
            )
        )
        reconciled += 1

    return reconciled


@celery_app.task(name="app.core.tasks.webhook_tasks.recover_orphaned_webhooks")
def recover_orphaned_webhooks() -> dict:
    """Periodic task to recover orphaned webhook events.

    Finds PENDING events with attempts=0 older than 5 minutes and republishes
    them via QStash. Also reconciles stale DELIVERING events when QStash logs
    show a terminal result but the callback did not update the database.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select as sa_select

    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.webhook import WebhookEvent, WebhookEventStatus
    from shared.services.webhook.qstash_publisher import get_qstash_webhook_publisher

    with periodic_task_lock(
        "app.core.tasks.webhook_tasks.recover_orphaned_webhooks",
        period_seconds=_WEBHOOK_RECOVERY_PERIOD_SECONDS,
    ) as acquired:
        if not acquired:
            return {"status": "skipped", "reason": "duplicate Beat firing"}

        logger.debug("Starting orphaned webhook recovery job")

        age_minutes = 5
        cutoff_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            minutes=age_minutes
        )
        recovered = 0
        reconciled = 0
        publisher = get_qstash_webhook_publisher()

        try:
            with get_sync_db_context() as db:
                stmt = (
                    sa_select(WebhookEvent)
                    .where(
                        WebhookEvent.status == WebhookEventStatus.PENDING,
                        WebhookEvent.attempts == 0,
                        WebhookEvent.created_at < cutoff_time,
                    )
                    .limit(100)
                )

                result = db.execute(stmt)
                orphaned_events = result.scalars().all()

                for event in orphaned_events:
                    try:
                        message_id = publisher.publish_event(event.id)
                        if not message_id:
                            logger.warning(
                                f"Orphaned webhook republish returned no message_id: {event.id}"
                            )
                            continue
                        recovered += 1
                        logger.info(
                            f"Recovered orphaned webhook event: {event.id}, "
                            f"message_id={message_id}"
                        )
                    except Exception as e:
                        logger.error(f"Error recovering webhook event {event.id}: {e}")

                reconciled = _reconcile_stale_delivering_events(
                    db,
                    publisher,
                    cutoff_time,
                )

            if recovered > 0:
                logger.bind(event=LogEvent.WORKER_TASK_COMPLETE.value).info(
                    f"Recovered {recovered} orphaned webhook events via QStash"
                )
            if reconciled > 0:
                logger.bind(event=LogEvent.WORKER_TASK_COMPLETE.value).info(
                    f"Reconciled {reconciled} stale webhook events from QStash logs"
                )
            if recovered == 0 and reconciled == 0:
                logger.debug("No orphaned or stale webhook events found")

            result = {"status": "success", "recovered": recovered, "provider": "qstash"}
            if reconciled:
                result["reconciled"] = reconciled
            return result

        except Exception as e:
            logger.error(f"Orphaned webhook recovery job failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}
