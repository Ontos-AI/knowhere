"""
QStash webhook publisher — delivers outbound webhooks via Upstash QStash.

Uses QStash's managed retry and callback infrastructure. Every publish includes:

- Our own HMAC-SHA256 signature (X-Knowhere-Signature) in the forwarded headers
- QStash callback + failure_callback pointing to our API
- Approximate exponential backoff retry (1m → 10m → ~100m → ~100m → ~100m)
- SSRF pre-validation before publishing
"""

from __future__ import annotations

from typing import Optional

from loguru import logger
from sqlalchemy import select

from shared.core.database_sync import get_sync_db_context
from shared.models.database.job import Job
from shared.models.database.webhook import WebhookEvent, WebhookEventStatus
from shared.services.webhook.qstash_client import (
    QStashClientAdapter,
    QStashDeliveryStatus,
)
from shared.services.webhook.qstash_payload import QStashPayloadEnricher
from shared.services.webhook.qstash_secret_resolver import QStashSecretResolver
from shared.services.webhook.signing import sign_webhook_payload
from shared.utils.url_security import (
    validate_http_url_and_resolve_ip,
)


class QStashWebhookPublisher:
    """Publishes webhook events to customer endpoints via QStash."""

    def __init__(
        self,
        *,
        client_adapter: QStashClientAdapter | None = None,
        payload_enricher: QStashPayloadEnricher | None = None,
        secret_resolver: QStashSecretResolver | None = None,
    ) -> None:
        self._client_adapter = client_adapter or QStashClientAdapter()
        self._payload_enricher = payload_enricher or QStashPayloadEnricher()
        self._secret_resolver = secret_resolver or QStashSecretResolver()

    def publish_event(self, event_id: str) -> Optional[str]:
        """Publish a webhook event via QStash.

        Fetches the WebhookEvent from the database, enriches the payload,
        signs it, and publishes via QStash with retry + callbacks.

        Returns the QStash message_id on success, or None on failure.
        """
        with get_sync_db_context() as db:
            event = db.execute(
                select(WebhookEvent).where(WebhookEvent.id == event_id)
            ).scalar_one_or_none()

            if not event:
                logger.warning(f"QStash publish: WebhookEvent not found: {event_id}")
                return None

            if event.is_terminal():
                logger.info(f"QStash publish: event already terminal: {event_id}")
                return None

            # SSRF pre-validation
            validation = validate_http_url_and_resolve_ip(
                event.target_url,
            )
            if not validation.is_valid:
                logger.warning(
                    f"QStash publish: SSRF validation failed for event {event_id}: "
                    f"{validation.error_message}"
                )
                event.status = WebhookEventStatus.FAILED
                db.commit()
                return None

            payload = self._payload_enricher.enrich(db, event)

            user_id = db.execute(
                select(Job.user_id).where(Job.job_id == event.job_id)
            ).scalar_one_or_none()

            if not user_id:
                logger.warning(f"QStash publish: no user_id for job {event.job_id}")
                event.status = WebhookEventStatus.FAILED
                db.commit()
                return None

            secret = self._secret_resolver.resolve(
                db,
                user_id=str(user_id),
                endpoint=event.target_url,
            )
            if not secret:
                logger.error(
                    f"QStash publish: secret resolution failed for event {event_id}"
                )
                event.status = WebhookEventStatus.FAILED
                db.commit()
                return None

            signature = sign_webhook_payload(payload, secret)

            try:
                message_id = self._client_adapter.publish_webhook(
                    target_url=event.target_url,
                    payload=payload,
                    signature=signature,
                    event_id=event_id,
                )
            except Exception as exc:
                logger.error(f"QStash publish failed for event {event_id}: {exc}")
                return None

            # Store QStash message_id on the event
            if message_id and hasattr(event, "qstash_message_id"):
                event.qstash_message_id = message_id
            event.status = WebhookEventStatus.DELIVERING
            db.commit()

            logger.info(
                f"QStash publish succeeded: event_id={event_id}, "
                f"qstash_message_id={message_id}"
            )
            return message_id

    def get_terminal_delivery_status(
        self,
        qstash_message_id: str,
    ) -> Optional[QStashDeliveryStatus]:
        """Read QStash logs for a terminal destination delivery state."""
        return self._client_adapter.get_terminal_delivery_status(qstash_message_id)


# Module-level singleton
_publisher: Optional[QStashWebhookPublisher] = None


def get_qstash_webhook_publisher() -> QStashWebhookPublisher:
    """Get the singleton QStash webhook publisher."""
    global _publisher
    if _publisher is None:
        _publisher = QStashWebhookPublisher()
    return _publisher
