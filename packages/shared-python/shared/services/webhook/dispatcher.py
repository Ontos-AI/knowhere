"""
Webhook Dispatcher Service

Dispatches webhook events via HTTP requests with HMAC signing and delivery logging.
Called by Celery task for async processing.
"""

import asyncio
import hashlib
import hmac
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Use standard db context - run_async_task handles the loop reuse
from shared.core.database import get_db_context
from shared.core.exceptions.domain_exceptions import (
    SystemSettingInvalidException,
    SystemSettingMissingException,
)
from shared.core.exceptions.webhook_exceptions import WebhookDeliveryException
from shared.models.database.job import Job
from shared.models.database.webhook import WebhookEvent, WebhookEventStatus
from shared.models.database.webhook_log import WebhookLog
from shared.services.jobs.result_delivery import JobResultDeliveryResolver
from shared.utils.pinned_outbound_http import (
    send_pinned_outbound_request,
)
from shared.utils.url_security import (
    HTTPURLValidationResult,
    validate_http_url_and_resolve_ip_async,
)

# Constants
HTTP_TIMEOUT_SECONDS = 10
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

            # 4. Dispatch the webhook
            # Logging is now handled inside _send_webhook
            success, status_code, duration_ms, error_message = await self._send_webhook(
                db=db, event=event, is_manual=False
            )

            # 6. Handle result (Logging already done)
            if success:
                await self._mark_delivered(db, event)
                return True  # Success
            else:
                # Determine if error is retryable
                # Retryable: 5xx, timeout (None), 429 (rate limit)
                # NOT retryable: 4xx (except 429) - client errors won't be fixed by retrying
                is_retryable = self._is_retryable_error(status_code)

                if not is_retryable:
                    # Permanent failure - don't retry
                    logger.warning(
                        f"WebhookEvent permanent failure (non-retryable): "
                        f"event_id={event_id}, status={status_code}"
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
                    internal_message=f"Webhook delivery failed: {error_message}",
                    retryable=True,
                    status_code=status_code,
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

    async def _send_webhook(
        self, db: AsyncSession, event: WebhookEvent, is_manual: bool = False
    ) -> Tuple[bool, Optional[int], int, Optional[str]]:
        """
        Send HTTP POST request to webhook target and log the attempt.

        Args:
            db: Database session
            event: WebhookEvent object
            is_manual: True if manually triggered (adds 'trigger': 'manual' to payload)

        Returns:
            Tuple of (success, status_code, duration_ms, error_message)
        """

        # Generate attempt ID
        attempt_id = str(uuid.uuid4())

        # SSRF Protection
        validation: HTTPURLValidationResult = await validate_http_url_and_resolve_ip_async(
            event.target_url,
        )
        if not validation.is_valid:
            logger.warning(
                f"SSRF validation failed: event_id={event.id}, error={validation.error_message}"
            )
            return False, 400, 0, f"SSRF: {validation.error_message}"

        # Enrich payload with job result data at delivery time
        enriched_payload = await self._enrich_payload(event)

        # Add manual mark if requested
        if is_manual:
            enriched_payload["trigger"] = "manual"

        # Helper to get user_id from job
        async def _get_job_owner(job_id: str) -> Optional[str]:
            result = await db.execute(select(Job.user_id).where(Job.job_id == job_id))
            return result.scalar_one_or_none()

        # Resolve secret (Lazy creation)
        secret = None
        try:
            user_id = await _get_job_owner(event.job_id)
            if user_id:
                secret = await self._resolve_secret(db, user_id, event.target_url)
            else:
                logger.warning(
                    f"Could not resolve secret: Job {event.job_id} has no user_id"
                )
        except (SystemSettingMissingException, SystemSettingInvalidException) as e:
            logger.error(f"Configuration error during secret resolution: {e}")
            # Return 424 (Failed Dependency) to ensure it's treated as a non-retryable error
            return False, 424, 0, f"Configuration Error: {e}"
        except Exception as e:
            logger.error(f"Secret resolution failed: {e}")

        if not secret:
            logger.error(f"No secret found or created/resolved for event {event.id}")
            # Default to non-retryable error for any secret resolution failure
            return False, 424, 0, "Secret resolution failed"

        # Sign payload
        signature = self._sign_payload(enriched_payload, secret)

        # Build headers
        headers = {
            "Content-Type": "application/json",
            "X-Knowhere-Signature": signature,
            "X-Knowhere-Attempt-ID": attempt_id,
            "User-Agent": "Knowhere-Webhook/1.0",
        }

        start_time = time.time()
        status_code = None
        error_message = None
        success = False

        try:
            pinned_ip = validation.validated_ip
            if not pinned_ip:
                return False, 400, 0, "SSRF validation did not return a pinned IP"

            response = await send_pinned_outbound_request(
                method="POST",
                url=event.target_url,
                pinned_ip=pinned_ip,
                timeout_seconds=HTTP_TIMEOUT_SECONDS,
                headers=headers,
                json_body=enriched_payload,
            )
            duration_ms = int((time.time() - start_time) * 1000)
            status_code = response.status

            if 200 <= response.status < 300:
                logger.info(
                    f"Webhook delivered: event_id={event.id}, status={response.status}"
                )
                success = True
            elif 300 <= response.status < 400:
                logger.warning(
                    f"Webhook redirect blocked (SSRF protection): "
                    f"event_id={event.id}, status={response.status}"
                )
                error_message = f"Redirect blocked: HTTP {response.status}"
                success = False
            else:
                logger.warning(
                    f"Webhook failed: event_id={event.id}, status={response.status}"
                )
                error_message = f"HTTP {response.status}"
                success = False

        except asyncio.TimeoutError:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"Webhook timeout: event_id={event.id}")
            error_message = "Connection timeout"
            success = False

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"Webhook error: event_id={event.id}, error={e}")
            error_message = str(e)
            success = False

        # Log delivery attempt
        # If manual, event_id is None to avoid FK violation
        log_event_id = None if is_manual else event.id

        try:
            # Combine headers and payload
            combined_payload = {"header": headers, "payload": enriched_payload}

            log = WebhookLog(
                job_id=event.job_id,
                event_id=log_event_id,
                webhook_url=event.target_url,
                attempt_number=event.attempts + 1,
                request_payload=combined_payload,
                signature=signature,
                idempotency_key=str(uuid.uuid4()),
                response_status_code=status_code,
                error_message=error_message,
                duration_ms=duration_ms,
            )
            db.add(log)
            # If auto-commit is needed?
            # Dispatcher.dispatch uses passed 'db' session which is managed by 'async with get_db_context()'.
            # It commits inside _mark_delivered etc.
            # We should probably commit/flush here to persist log even if update fails?
            await db.commit()

        except Exception as e:
            logger.error(f"Failed to log webhook delivery: {e}")

        return success, status_code, duration_ms, error_message

    async def _enrich_payload(self, event: WebhookEvent) -> Dict[str, Any]:
        """
        Enrich webhook payload with job result data at delivery time.

        For job.completed events:
        - Adds result_url (fresh download URL for result zip)
        - Adds result (inline payload with checksum/statistics)

        This ensures download URLs are generated fresh (they expire)
        and data is current at delivery time.
        """
        payload = dict(event.payload)  # Copy to avoid mutating stored payload

        # Only enrich completion events
        if payload.get("event") != "job.completed":
            return payload

        try:
            # Fetch job with result
            from sqlalchemy.orm import selectinload

            from shared.models.database.job import Job

            async with get_db_context() as db:
                result = await db.execute(
                    select(Job)
                    .options(selectinload(Job.job_result))
                    .where(Job.job_id == event.job_id)
                )
                job = result.scalar_one_or_none()

                if not job or not job.job_result:
                    logger.warning(
                        f"Job or result not found for enrichment: job_id={event.job_id}"
                    )
                    return payload

                payload = JobResultDeliveryResolver().enrich_payload(
                    payload,
                    job_result=job.job_result,
                )

        except Exception as e:
            logger.error(f"Failed to enrich payload for event {event.id}: {e}")
            # Continue with original payload if enrichment fails

        return payload

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

    async def _resolve_secret(
        self, db: AsyncSession, user_id: str, endpoint: str
    ) -> Optional[str]:
        """
        Resolve webhook secret using repository (Lazy creation).

        1. Try to get existing active secret for user/endpoint.
        2. If not found, create a new one.
        3. Decrypt and return the raw secret string.
        """
        try:
            # Import here to avoid circular dependency with WebhookDispatcher
            from shared.repositories.webhook_secret_repository import (
                WebhookSecretRepository,
            )

            repo = WebhookSecretRepository()
            secret_obj = await repo.get_or_create_secret(db, user_id, endpoint=endpoint)

            # Update usage timestamp
            if secret_obj:
                secret_obj.last_used_at = datetime.now(timezone.utc).replace(
                    tzinfo=None
                )
                db.add(secret_obj)
                # We don't commit here to avoid side effects if the caller aborts,
                # but the session will eventually be committed by the caller.

            # Decrypt
            return repo.decrypt_secret(secret_obj)
        except (SystemSettingMissingException, SystemSettingInvalidException):
            # Re-raise configuration errors so they can be handled as non-retryable
            raise
        except Exception as e:
            logger.error(f"Failed to resolve/create secret for user {user_id}: {e}")
            return None

    def _sign_payload(self, payload: Dict[str, Any], secret: str) -> str:
        """
        Generate timestamped HMAC-SHA256 signature.

        Format: t=<timestamp>,v1=<signature>
        Signed content: "{timestamp}.{json_payload}"

        This prevents replay attacks by binding the signature to the current time.
        """
        timestamp = int(time.time())
        payload_str = json.dumps(payload, separators=(",", ":"))
        signed_content = f"{timestamp}.{payload_str}"

        signature = hmac.new(
            secret.encode("utf-8"), signed_content.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        return f"t={timestamp},v1={signature}"

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
