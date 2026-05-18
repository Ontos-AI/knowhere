"""Pinned HTTP delivery for outbound webhooks."""

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from loguru import logger

from shared.utils.pinned_outbound_http import send_pinned_outbound_request
from shared.utils.url_security import validate_http_url_and_resolve_ip_async

HTTP_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class WebhookDeliveryTarget:
    target_url: str
    pinned_ip: str


@dataclass(frozen=True)
class WebhookDeliveryResult:
    success: bool
    status_code: int | None
    duration_ms: int
    error_message: str | None


@dataclass(frozen=True)
class WebhookTargetValidation:
    target: WebhookDeliveryTarget | None
    failure: WebhookDeliveryResult | None


class WebhookDeliveryClient:
    """Validate and send direct webhook HTTP requests with DNS pinning."""

    async def validate_target(
        self, *, event_id: str, target_url: str
    ) -> WebhookTargetValidation:
        validation = await validate_http_url_and_resolve_ip_async(target_url)

        if not validation.is_valid:
            logger.warning(
                f"SSRF validation failed: event_id={event_id}, "
                f"error={validation.error_message}"
            )
            return WebhookTargetValidation(
                target=None,
                failure=WebhookDeliveryResult(
                    success=False,
                    status_code=400,
                    duration_ms=0,
                    error_message=f"SSRF: {validation.error_message}",
                ),
            )

        if not validation.validated_ip:
            return WebhookTargetValidation(
                target=None,
                failure=WebhookDeliveryResult(
                    success=False,
                    status_code=400,
                    duration_ms=0,
                    error_message="SSRF validation did not return a pinned IP",
                ),
            )

        return WebhookTargetValidation(
            target=WebhookDeliveryTarget(
                target_url=target_url,
                pinned_ip=validation.validated_ip,
            ),
            failure=None,
        )

    async def post_json(
        self,
        *,
        event_id: str,
        target: WebhookDeliveryTarget,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> WebhookDeliveryResult:
        start_time = time.time()

        try:
            response = await send_pinned_outbound_request(
                method="POST",
                url=target.target_url,
                pinned_ip=target.pinned_ip,
                timeout_seconds=HTTP_TIMEOUT_SECONDS,
                headers=headers,
                json_body=payload,
            )
            duration_ms = int((time.time() - start_time) * 1000)

            if 200 <= response.status < 300:
                logger.info(
                    f"Webhook delivered: event_id={event_id}, status={response.status}"
                )
                return WebhookDeliveryResult(
                    success=True,
                    status_code=response.status,
                    duration_ms=duration_ms,
                    error_message=None,
                )

            if 300 <= response.status < 400:
                logger.warning(
                    f"Webhook redirect blocked (SSRF protection): "
                    f"event_id={event_id}, status={response.status}"
                )
                return WebhookDeliveryResult(
                    success=False,
                    status_code=response.status,
                    duration_ms=duration_ms,
                    error_message=f"Redirect blocked: HTTP {response.status}",
                )

            logger.warning(
                f"Webhook failed: event_id={event_id}, status={response.status}"
            )
            return WebhookDeliveryResult(
                success=False,
                status_code=response.status,
                duration_ms=duration_ms,
                error_message=f"HTTP {response.status}",
            )

        except asyncio.TimeoutError:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"Webhook timeout: event_id={event_id}")
            return WebhookDeliveryResult(
                success=False,
                status_code=None,
                duration_ms=duration_ms,
                error_message="Connection timeout",
            )

        except Exception as error:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"Webhook error: event_id={event_id}, error={error}")
            return WebhookDeliveryResult(
                success=False,
                status_code=None,
                duration_ms=duration_ms,
                error_message=str(error),
            )
