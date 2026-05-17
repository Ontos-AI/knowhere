"""
Webhook Dispatcher Service

Dispatches webhook events with retry policy. Direct HTTP delivery details live
behind WebhookEventDelivery.
"""

import threading
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Use standard db context - run_async_task handles the loop reuse
from shared.core.database import get_db_context
from shared.core.exceptions.webhook_exceptions import WebhookDeliveryException
from shared.models.database.webhook import WebhookEvent, WebhookEventStatus
from shared.services.webhook.delivery_client import WebhookDeliveryResult
from shared.services.webhook.event_delivery import WebhookEventDelivery

# Constants
MAX_ATTEMPTS = 6


class WebhookDispatcher:
    """
    Webhook Dispatcher - Consumes events and sends HTTP requests.

    Implements the consumer side of the Transactional Outbox Pattern:
    1. Fetches WebhookEvent from database
    2. Checks if terminal or max attempts exceeded
    3. Signs payload and sends HTTP request
    4. Logs delivery attempt
    5. On failure, signals the caller to schedule retry
    """

    def __init__(self, event_delivery: WebhookEventDelivery | None = None) -> None:
        self._event_delivery = event_delivery or WebhookEventDelivery()

    async def dispatch(self, event_id: str) -> bool:
        """
        Dispatch a webhook event.

        Args:
            event_id: The WebhookEvent ID to dispatch

        Returns:
            True if successfully dispatched or terminal, False if should retry
        """
        async with get_db_context() as db:
            # 1. Fetch event from database
            event = await self._fetch_event(db, event_id)

            if not event:
                logger.warning(f"WebhookEvent not found: {event_id}")
                return True  # ACK - event doesn't exist

            # 2. Check if already terminal (idempotency)
            if event.is_terminal():
                logger.info(
                    f"WebhookEvent already terminal: {event_id}, status={event.status}"
                )
                return True  # ACK

            # 3. Check max attempts
            if event.attempts >= MAX_ATTEMPTS:
                logger.warning(
                    f"WebhookEvent max attempts exceeded: {event_id}, attempts={event.attempts}"
                )
                await self._mark_failed(db, event)
                return True  # ACK

            delivery_result = await self._event_delivery.send(
                db=db, event=event, is_manual=False
            )

            if delivery_result.success:
                await self._mark_delivered(db, event)
                return True  # Success
            else:
                # Determine if error is retryable
                # Retryable: 5xx, timeout (None), 429 (rate limit)
                # NOT retryable: 4xx (except 429) - client errors won't be fixed by retrying
                is_retryable = self._is_retryable_error(delivery_result.status_code)

                if not is_retryable:
                    # Permanent failure - don't retry
                    logger.warning(
                        f"WebhookEvent permanent failure (non-retryable): "
                        f"event_id={event_id}, status={delivery_result.status_code}"
                    )
                    await self._mark_failed(db, event)
                    return True  # ACK - no point retrying

                # Increment attempts for transient errors
                await self._increment_attempts(db, event)

                if event.attempts >= MAX_ATTEMPTS:
                    await self._mark_failed(db, event)
                    return True  # Final failure, no more retries

                # Raise exception so Celery task will retry
                raise WebhookDeliveryException(
                    internal_message=(
                        f"Webhook delivery failed: {delivery_result.error_message}"
                    ),
                    retryable=True,
                    status_code=delivery_result.status_code,
                )

    async def mark_event_failed(self, event_id: str) -> None:
        """
        Mark a webhook event as permanently failed (public helper).
        Used by Celery task on_failure or when retries exhausted.
        """
        async with get_db_context() as db:
            event = await self._fetch_event(db, event_id)
            if event:
                await self._mark_failed(db, event)
            else:
                logger.warning(f"Cannot mark failed: WebhookEvent {event_id} not found")

    async def _fetch_event(
        self, db: AsyncSession, event_id: str
    ) -> Optional[WebhookEvent]:
        """Fetch WebhookEvent by ID."""
        result = await db.execute(
            select(WebhookEvent).where(WebhookEvent.id == event_id)
        )
        return result.scalar_one_or_none()

    async def send_manual_webhook(
        self, db: AsyncSession, event: WebhookEvent
    ) -> WebhookDeliveryResult:
        """Send a webhook immediately for an operator-triggered retry."""
        return await self._event_delivery.send(db=db, event=event, is_manual=True)

    def _is_retryable_error(self, status_code: Optional[int]) -> bool:
        """
        Determine if an HTTP error is transient and worth retrying.

        Retryable (transient) errors:
        - None (timeout/network error)
        - 429 (rate limited)
        - 500-599 (server errors)

        NOT retryable (permanent) errors:
        - 400-428, 430-499 (client errors - won't be fixed by retrying)

        Args:
            status_code: HTTP response status code, or None for timeout/network

        Returns:
            True if error is transient and should be retried
        """
        if status_code is None:
            # Timeout or network error - always retry
            return True

        if status_code == 429:
            # Rate limited - retry after backoff
            return True

        if 500 <= status_code < 600:
            # Server error - transient, retry
            return True

        # 4xx (except 429) - client error, permanent failure
        # Examples: 400 Bad Request, 401 Unauthorized, 404 Not Found
        return False

    async def _mark_delivered(self, db: AsyncSession, event: WebhookEvent) -> None:
        """Mark event as delivered."""
        event.status = WebhookEventStatus.DELIVERED
        event.attempts += 1
        event.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
        logger.info(f"WebhookEvent delivered: {event.id}")

    async def _mark_failed(self, db: AsyncSession, event: WebhookEvent) -> None:
        """Mark event as failed (max retries exceeded)."""
        event.status = WebhookEventStatus.FAILED
        event.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
        logger.warning(f"WebhookEvent failed permanently: {event.id}")

    async def _increment_attempts(self, db: AsyncSession, event: WebhookEvent) -> None:
        """Increment attempt count for failed delivery (Celery will retry)."""
        event.attempts += 1
        event.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db.commit()
        logger.info(
            f"WebhookEvent attempt incremented: {event.id}, attempts={event.attempts}"
        )


# Singleton instance
_dispatcher: Optional[WebhookDispatcher] = None
_dispatcher_lock = threading.Lock()


def get_webhook_dispatcher() -> WebhookDispatcher:
    """Get the singleton WebhookDispatcher instance."""
    global _dispatcher
    if _dispatcher is None:
        with _dispatcher_lock:
            if _dispatcher is None:
                _dispatcher = WebhookDispatcher()
    return _dispatcher
