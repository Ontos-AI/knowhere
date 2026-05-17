from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

from shared.core.config import app_config
from shared.core.exceptions.domain_exceptions import QStashServiceException
from shared.models.database.webhook import WebhookEventStatus


@dataclass(frozen=True)
class QStashDeliveryStatus:
    """Terminal delivery status observed from QStash logs."""

    status: str
    response_status_code: Optional[int]
    response_body: Optional[str]
    error_message: Optional[str]


class QStashClientAdapter:
    """Upstash QStash client adapter for webhook publication and log lookup."""

    def __init__(self) -> None:
        self._client: Any = None

    def get_client(self) -> Any:
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

    def publish_webhook(
        self,
        *,
        target_url: str,
        payload: dict[str, Any],
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

        publish_kwargs: dict[str, Any] = {
            "url": target_url,
            "body": json.dumps(payload, separators=(",", ":")),
            "headers": headers,
            "retries": app_config.QSTASH_MAX_RETRIES,
            "content_type": "application/json",
            "retry_delay": _get_retry_delay_expression(),
            "callback": callback_url,
            "failure_callback": failure_callback_url,
            "deduplication_id": event_id,
            "label": "knowhere-webhook",
        }

        response = self.get_client().message.publish(**publish_kwargs)

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

            response = self.get_client().log.list(
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


def _get_retry_delay_expression() -> str:
    # Approximate exponential backoff: 1m, 10m, ~100m, ~100m, ~100m.
    return "pow(10, min(retried, 2)) * 60000"
