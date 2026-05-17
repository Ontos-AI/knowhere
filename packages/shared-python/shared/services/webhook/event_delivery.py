"""Direct WebhookEvent delivery attempt orchestration."""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from shared.core.exceptions.domain_exceptions import (
    SystemSettingInvalidException,
    SystemSettingMissingException,
)
from shared.models.database.webhook import WebhookEvent
from shared.models.database.webhook_log import WebhookLog
from shared.services.webhook.delivery_client import (
    WebhookDeliveryClient,
    WebhookDeliveryResult,
)
from shared.services.webhook.payload_enrichment import WebhookPayloadEnricher
from shared.services.webhook.secret_resolver import WebhookSecretResolver
from shared.services.webhook.signing import build_webhook_headers


class WebhookEventDelivery:
    """Send one direct webhook attempt and persist its delivery log."""

    def __init__(
        self,
        *,
        client: WebhookDeliveryClient | None = None,
        enricher: WebhookPayloadEnricher | None = None,
        secret_resolver: WebhookSecretResolver | None = None,
    ) -> None:
        self._client = client or WebhookDeliveryClient()
        self._enricher = enricher or WebhookPayloadEnricher()
        self._secret_resolver = secret_resolver or WebhookSecretResolver()

    async def send(
        self, *, db: AsyncSession, event: WebhookEvent, is_manual: bool = False
    ) -> WebhookDeliveryResult:
        attempt_id = str(uuid.uuid4())
        target_validation = await self._client.validate_target(
            event_id=event.id,
            target_url=event.target_url,
        )
        if target_validation.failure:
            return target_validation.failure
        if not target_validation.target:
            return WebhookDeliveryResult(
                success=False,
                status_code=400,
                duration_ms=0,
                error_message="Webhook target validation failed",
            )

        payload = await self._enricher.enrich(event)
        if is_manual:
            payload["trigger"] = "manual"

        secret, secret_error = await self._resolve_secret(db, event)
        if not secret:
            logger.error(f"No secret found or created/resolved for event {event.id}")
            return WebhookDeliveryResult(
                success=False,
                status_code=424,
                duration_ms=0,
                error_message=secret_error or "Secret resolution failed",
            )

        headers = build_webhook_headers(
            payload=payload,
            secret=secret,
            attempt_id=attempt_id,
        )
        result = await self._client.post_json(
            event_id=event.id,
            target=target_validation.target,
            payload=payload,
            headers=headers,
        )
        await self._log_attempt(
            db=db,
            event=event,
            is_manual=is_manual,
            headers=headers,
            payload=payload,
            result=result,
        )
        return result

    async def _resolve_secret(
        self, db: AsyncSession, event: WebhookEvent
    ) -> tuple[str | None, str | None]:
        try:
            return await self._secret_resolver.resolve_for_event(db, event), None
        except (SystemSettingMissingException, SystemSettingInvalidException) as error:
            logger.error(f"Configuration error during secret resolution: {error}")
            return None, f"Configuration Error: {error}"

    async def _log_attempt(
        self,
        *,
        db: AsyncSession,
        event: WebhookEvent,
        is_manual: bool,
        headers: dict[str, str],
        payload: dict[str, Any],
        result: WebhookDeliveryResult,
    ) -> None:
        try:
            log = WebhookLog(
                job_id=event.job_id,
                event_id=None if is_manual else event.id,
                webhook_url=event.target_url,
                attempt_number=event.attempts + 1,
                request_payload={"header": headers, "payload": payload},
                signature=headers["X-Knowhere-Signature"],
                idempotency_key=str(uuid.uuid4()),
                response_status_code=result.status_code,
                error_message=result.error_message,
                duration_ms=result.duration_ms,
            )
            db.add(log)
            await db.commit()

        except Exception as error:
            logger.error(f"Failed to log webhook delivery: {error}")
