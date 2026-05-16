"""
QStash webhook publisher — delivers outbound webhooks via Upstash QStash.

Uses QStash's managed retry and callback infrastructure. Every publish includes:

- Our own HMAC-SHA256 signature (X-Knowhere-Signature) in the forwarded headers
- QStash callback + failure_callback pointing to our API
- Approximate exponential backoff retry (1m → 10m → ~100m → ~100m → ~100m)
- SSRF pre-validation before publishing
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from loguru import logger

from shared.core.config import app_config
from shared.core.exceptions.domain_exceptions import QStashServiceException
from shared.models.database.webhook import WebhookEventStatus
from shared.services.jobs.result_delivery import JobResultDeliveryResolver
from shared.utils.url_security import (
    validate_http_url_and_resolve_ip,
)


@dataclass(frozen=True)
class QStashDeliveryStatus:
    """Terminal delivery status observed from QStash logs."""

    status: str
    response_status_code: Optional[int]
    response_body: Optional[str]
    error_message: Optional[str]


class QStashWebhookPublisher:
    """Publishes webhook events to customer endpoints via QStash."""

    def __init__(self) -> None:
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazily initialize the QStash client."""
        if self._client is None:
            try:
                from qstash import QStash
            except ImportError as exc:
                raise QStashServiceException(
                    internal_message=(
                        "qstash package is required for QStash webhook delivery. "
                        "Install it with: pip install qstash"
                    ),
                    operation="initialize_client",
                    original_exception=exc,
                ) from exc

            token = app_config.QSTASH_TOKEN
            if not token:
                raise QStashServiceException(
                    internal_message="QSTASH_TOKEN is not configured",
                    operation="initialize_client",
                )

            self._client = QStash(token, base_url=app_config.QSTASH_BASE_URL)
        return self._client

    def publish_event(self, event_id: str) -> Optional[str]:
        """Publish a webhook event via QStash.

        Fetches the WebhookEvent from the database, enriches the payload,
        signs it, and publishes via QStash with retry + callbacks.

        Returns the QStash message_id on success, or None on failure.
        """
        from sqlalchemy import select

        from shared.core.database_sync import get_sync_db_context
        from shared.models.database.job import Job
        from shared.models.database.webhook import WebhookEvent

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

            # Enrich payload (presigned S3 URL for completed jobs)
            payload = self._enrich_payload(db, event)

            # Resolve signing secret
            user_id = db.execute(
                select(Job.user_id).where(Job.job_id == event.job_id)
            ).scalar_one_or_none()

            if not user_id:
                logger.warning(f"QStash publish: no user_id for job {event.job_id}")
                event.status = WebhookEventStatus.FAILED
                db.commit()
                return None

            secret = self._resolve_secret(db, str(user_id), event.target_url)
            if not secret:
                logger.error(
                    f"QStash publish: secret resolution failed for event {event_id}"
                )
                event.status = WebhookEventStatus.FAILED
                db.commit()
                return None

            # Sign payload with our HMAC
            signature = self._sign_payload(payload, secret)

            # Publish to QStash
            try:
                message_id = self._publish_to_qstash(
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

    def _publish_to_qstash(
        self,
        target_url: str,
        payload: Dict[str, Any],
        signature: str,
        event_id: str,
    ) -> Optional[str]:
        """Call the QStash publish API."""
        headers = {
            "Content-Type": "application/json",
            "X-Knowhere-Signature": signature,
            "X-Knowhere-Event-ID": event_id,
            "User-Agent": "Knowhere-Webhook/1.0",
        }

        # Approximate exponential backoff: 1m, 10m, ~100m, ~100m, ~100m
        # pow(10, min(retried, 2)) * 60000 → 60s, 600s, 6000s capped
        retry_delay_expression = "pow(10, min(retried, 2)) * 60000"

        callback_url = app_config.qstash_callback_url
        failure_callback_url = app_config.qstash_failure_callback_url
        if not callback_url or not failure_callback_url:
            raise QStashServiceException(
                internal_message=(
                    "QSTASH_CALLBACK_BASE_URL must be configured for QStash "
                    "webhook delivery"
                ),
                operation="publish_webhook",
            )

        client = self._get_client()

        publish_kwargs: Dict[str, Any] = {
            "url": target_url,
            "body": json.dumps(payload, separators=(",", ":")),
            "headers": headers,
            "retries": app_config.QSTASH_MAX_RETRIES,
            "content_type": "application/json",
            "retry_delay": retry_delay_expression,
            "callback": callback_url,
            "failure_callback": failure_callback_url,
            "deduplication_id": event_id,
            "label": "knowhere-webhook",
        }

        response = client.message.publish(**publish_kwargs)

        message_id = getattr(response, "message_id", None)
        if message_id is None and isinstance(response, dict):
            message_id = response.get("messageId") or response.get("message_id")

        return message_id

    def get_terminal_delivery_status(
        self,
        qstash_message_id: str,
    ) -> Optional[QStashDeliveryStatus]:
        """Read QStash logs for a terminal destination delivery state."""
        try:
            from qstash.log import LogState

            response = self._get_client().log.list(
                filter={"message_id": qstash_message_id},
                count=20,
            )
        except Exception as exc:
            logger.warning(
                f"QStash delivery status lookup failed: "
                f"message_id={qstash_message_id}, error={exc}"
            )
            return None

        terminal_logs = sorted(response.logs, key=lambda log: log.time, reverse=True)
        for log in terminal_logs:
            if log.state == LogState.DELIVERED:
                return QStashDeliveryStatus(
                    status=WebhookEventStatus.DELIVERED,
                    response_status_code=log.response_status,
                    response_body=log.response_body,
                    error_message=log.error,
                )

            if log.state == LogState.FAILED:
                return QStashDeliveryStatus(
                    status=WebhookEventStatus.FAILED,
                    response_status_code=log.response_status,
                    response_body=log.response_body,
                    error_message=log.error,
                )

        return None

    def _enrich_payload(self, db: Any, event: Any) -> Dict[str, Any]:
        """Enrich the webhook payload (e.g., generate fresh presigned S3 URL)."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from shared.models.database.job import Job

        payload = dict(event.payload)
        if payload.get("event") != "job.completed":
            return payload

        try:
            result = db.execute(
                select(Job)
                .options(selectinload(Job.job_result))
                .where(Job.job_id == event.job_id)
            )
            job = result.scalar_one_or_none()
            if not job or not job.job_result:
                return payload

            payload = JobResultDeliveryResolver().enrich_payload(
                payload,
                job_result=job.job_result,
            )
        except Exception as exc:
            logger.error(f"Failed to enrich payload for event {event.id}: {exc}")

        return payload

    def _resolve_secret(self, db: Any, user_id: str, endpoint: str) -> Optional[str]:
        """Resolve the webhook signing secret for a user/endpoint."""
        from datetime import datetime, timezone

        from sqlalchemy import and_, select

        from shared.core.exceptions.domain_exceptions import (
            SystemSettingInvalidException,
            SystemSettingMissingException,
        )
        from shared.models.database.webhook_secret import (
            WebhookSecret,
            WebhookSecretStatus,
        )
        from shared.services.encryption import get_fernet_service

        try:
            fernet = get_fernet_service()
        except (SystemSettingMissingException, SystemSettingInvalidException) as exc:
            logger.error(f"Configuration error during secret resolution: {exc}")
            return None

        # Try endpoint-specific secret first, then global
        secret_obj = None
        if endpoint:
            result = db.execute(
                select(WebhookSecret).where(
                    and_(
                        WebhookSecret.user_id == user_id,
                        WebhookSecret.endpoint == endpoint,
                        WebhookSecret.status == WebhookSecretStatus.ACTIVE,
                    )
                )
            )
            secret_obj = result.scalar_one_or_none()

        if secret_obj is None:
            result = db.execute(
                select(WebhookSecret).where(
                    and_(
                        WebhookSecret.user_id == user_id,
                        WebhookSecret.endpoint.is_(None),
                        WebhookSecret.status == WebhookSecretStatus.ACTIVE,
                    )
                )
            )
            secret_obj = result.scalar_one_or_none()

        if secret_obj is None:
            raw_secret = fernet.generate_webhook_secret()
            secret_obj = WebhookSecret(
                user_id=user_id,
                endpoint=endpoint,
                secret_encrypted=fernet.encrypt(raw_secret),
                status=WebhookSecretStatus.ACTIVE,
            )
            db.add(secret_obj)
            db.commit()
            db.refresh(secret_obj)

        secret_obj.last_used_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(secret_obj)
        return fernet.decrypt(secret_obj.secret_encrypted)

    @staticmethod
    def _sign_payload(payload: Dict[str, Any], secret: str) -> str:
        """Generate HMAC-SHA256 signature matching the existing Knowhere format."""
        timestamp = int(time.time())
        payload_str = json.dumps(payload, separators=(",", ":"))
        signed_content = f"{timestamp}.{payload_str}"
        sig = hmac.new(
            secret.encode("utf-8"),
            signed_content.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"t={timestamp},v1={sig}"


# Module-level singleton
_publisher: Optional[QStashWebhookPublisher] = None


def get_qstash_webhook_publisher() -> QStashWebhookPublisher:
    """Get the singleton QStash webhook publisher."""
    global _publisher
    if _publisher is None:
        _publisher = QStashWebhookPublisher()
    return _publisher
