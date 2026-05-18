"""SNS subscription confirmation handling."""
from __future__ import annotations

from loguru import logger

from shared.services.http.pinned_outbound import send_pinned_outbound_request
from shared.services.http.url_security import validate_http_url_and_resolve_ip_async


SNS_SUBSCRIPTION_TIMEOUT_SECONDS = 10


async def confirm_sns_subscription(subscribe_url: str) -> dict[str, str]:
    validation = await validate_http_url_and_resolve_ip_async(subscribe_url)
    if not validation.is_valid:
        logger.warning(
            f"SNS subscription confirmation URL failed validation: {validation.error_message}"
        )
        return {"message": "SNS subscription confirmation failed"}

    if not validation.validated_ip:
        logger.warning("SNS subscription confirmation URL validation returned no IP")
        return {"message": "SNS subscription confirmation failed"}

    try:
        response = await send_pinned_outbound_request(
            method="GET",
            url=subscribe_url,
            pinned_ip=validation.validated_ip,
            timeout_seconds=SNS_SUBSCRIPTION_TIMEOUT_SECONDS,
        )
        if response.status == 200:
            logger.info("SNS subscription confirmed successfully")
            return {"message": "SNS subscription confirmed"}

        if 300 <= response.status < 400:
            logger.warning(
                f"SNS subscription confirmation redirect blocked, status={response.status}"
            )
        else:
            logger.error(
                f"SNS subscription confirmation failed, status={response.status}"
            )
        return {"message": "SNS subscription confirmation failed"}
    except Exception as exc:
        logger.error(f"Failed to reach the SNS confirmation URL: {exc}")
        return {"message": "SNS subscription confirmation failed"}
