from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.database.job import Job
from shared.models.database.webhook import WebhookEvent, WebhookEventStatus


@dataclass(frozen=True)
class WebhookOutboxEvent:
    event_id: str


class SyncJobWebhookOutbox:
    """Create webhook events in-transaction and publish them after commit."""

    def create_event(
        self,
        db: Session,
        *,
        job_id: str,
        event_type: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> WebhookOutboxEvent | None:
        result = db.execute(select(Job).where(Job.job_id == job_id))
        job = result.scalar_one_or_none()

        if not job:
            logger.warning(f"Job not found for webhook check: {job_id}")
            return None

        webhook_url = getattr(job, "webhook_url", None)
        if not job.webhook_enabled or not webhook_url:
            return None

        status = "completed" if event_type == "job.completed" else "failed"
        timestamp_key = f"{status}_at"
        payload: dict[str, Any] = {
            "event": event_type,
            "job_id": job_id,
            "status": status,
            timestamp_key: _utc_now_naive().isoformat(),
        }
        if extra_payload:
            payload.update(extra_payload)

        event = WebhookEvent(
            job_id=job_id,
            target_url=webhook_url,
            payload=payload,
            status=WebhookEventStatus.PENDING,
            attempts=0,
        )
        db.add(event)
        db.flush()
        logger.info(f"WebhookEvent created: event_id={event.id}, job_id={job_id}")
        return WebhookOutboxEvent(event_id=event.id)

    def enqueue_after_commit(self, webhook_event: WebhookOutboxEvent | None) -> None:
        if not webhook_event:
            return
        self.enqueue_event_id_after_commit(webhook_event.event_id)

    def enqueue_event_id_after_commit(self, webhook_event_id: str | None) -> None:
        if not webhook_event_id:
            return
        try:
            from shared.services.webhook.qstash_publisher import (
                get_qstash_webhook_publisher,
            )

            publisher = get_qstash_webhook_publisher()
            message_id = publisher.publish_event(webhook_event_id)
            if not message_id:
                logger.warning(
                    f"Webhook publish failed after commit: event_id={webhook_event_id}"
                )
                return
            logger.info(
                f"Webhook published after commit: event_id={webhook_event_id}, "
                f"message_id={message_id}"
            )
        except Exception as exc:
            logger.error(
                "Failed to publish webhook after commit (event persisted): "
                f"event_id={webhook_event_id}, error={exc}"
            )


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
