from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from functools import partial

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.database.webhook import WebhookEvent, WebhookEventStatus
from shared.core.response import build_standard_error_response
from shared.services.background import BackgroundWorkPool
from shared.services.jobs.progressive_ingestion import read_progressive_state

_DISPATCH_POOL: BackgroundWorkPool | None = None
_COMPLETED_EVENTS: frozenset[str] = frozenset({
    "job.completed",
    "job.enrichment.completed",
    "job.document.directory_ready",
})
_DEFAULT_EVENTS: frozenset[str] = _COMPLETED_EVENTS | {"job.failed"}


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
        event_id: str | None = None,
    ) -> WebhookOutboxEvent | None:
        job: Job | None = db.scalar(select(Job).where(Job.job_id == job_id).with_for_update())

        if job is None:
            logger.warning(f"Job not found for webhook check: {job_id}")
            return None

        webhook_url, webhook_enabled = job.webhook_url, job.webhook_enabled
        if not webhook_enabled or not webhook_url:
            return None
        subscribed = job.webhook_events if job.webhook_events is not None else _DEFAULT_EVENTS
        if event_type not in subscribed:
            return None
        is_indexing_failure: bool = event_type == "job.failed" and job.failure_stage == "indexing"
        milestone_key: str = f"{job_id}:{event_type}:{job.current_indexing_attempt_id if is_indexing_failure else 'job'}"
        previous = select(WebhookEvent).where(
            WebhookEvent.job_id == job_id,
            WebhookEvent.payload["event"].astext == event_type,
        )
        if is_indexing_failure:
            previous = previous.where(
                WebhookEvent.payload["indexing_attempt_id"].astext == job.current_indexing_attempt_id
            )
        existing: WebhookEvent | None = db.scalars(previous.limit(1)).first()
        if existing is not None:
            return WebhookOutboxEvent(event_id=existing.id)

        if event_type == "job.parsed":
            status, timestamp_key = "running", "parsed_at"
        elif event_type in _COMPLETED_EVENTS:
            status, timestamp_key = "completed", "completed_at"
        else:
            status, timestamp_key = "failed", "failed_at"
        event_id = event_id or str(uuid4())
        result = db.execute(
            select(JobResult.id, JobResult.document_metadata).where(JobResult.job_id == job_id)
        ).one_or_none()
        payload: dict[str, Any] = {
            "event_id": event_id,
            "event": event_type,
            "job_id": job_id,
            "status": status,
            timestamp_key: _utc_now_naive().isoformat(),
            "parsing_status": job.parsing_status,
            "indexing_status": job.indexing_status,
            "failure_stage": job.failure_stage,
            "indexing_attempt_id": job.current_indexing_attempt_id,
            "job_result_id": result.id if result else None,
        }
        if event_type == "job.completed" and result is not None:
            progressive_state = read_progressive_state(result.document_metadata)
            baseline = (
                next((item for item in progressive_state.snapshots if item.stage == "baseline"), None)
                if progressive_state is not None
                else None
            )
            if baseline is not None:
                payload.update(
                    document_id=baseline.document_id,
                    snapshot_id=baseline.snapshot_id,
                    checksum=baseline.checksum,
                )
        if event_type == "job.failed" and job.error_message:
            payload["error"] = build_standard_error_response(code=job.error_code or "INTERNAL_ERROR",
                message=job.error_message, request_id=job_id,
                details=dict(job.job_metadata or {}).get("error_details"))
        if extra_payload:
            payload.update(extra_payload)

        event = WebhookEvent(
            id=event_id,
            milestone_key=milestone_key,
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
        global _DISPATCH_POOL
        try:
            if _DISPATCH_POOL is None:
                _DISPATCH_POOL = BackgroundWorkPool(name="webhook-outbox", maximumWorkers=2)
            if not _DISPATCH_POOL.trySubmit(partial(self._publishEvent, webhook_event_id)):
                logger.warning("Webhook dispatch deferred; durable event retained: event_id={}", webhook_event_id)
        except Exception as error:
            logger.error("Webhook submission deferred: event_id={}, error_type={}", webhook_event_id, type(error).__name__)

    @staticmethod
    def close() -> None:
        global _DISPATCH_POOL
        if _DISPATCH_POOL is not None:
            _DISPATCH_POOL.close()
            _DISPATCH_POOL = None

    def _publishEvent(self, webhook_event_id: str) -> None:
        try:
            from shared.services.webhook.qstash_publisher import (
                QStashWebhookPublisher,
            )

            # Each native operation owns its transport. A global HTTPX pool can
            # retain gevent sockets belonging to a retired background thread.
            publisher = QStashWebhookPublisher()
            try:
                message_id = publisher.publish_event(webhook_event_id)
            finally:
                publisher.close()
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
