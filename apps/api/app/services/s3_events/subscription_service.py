"""SNS subscription confirmation handling."""
from __future__ import annotations

import asyncio
import socket
from collections.abc import Sequence

from loguru import logger
from urllib.parse import ParseResult, parse_qs, urlparse, urlunparse

from shared.core.config import settings
from shared.services.http.pinned_outbound import send_pinned_outbound_request
from shared.services.http.url_security import (
    HTTPURLValidationResult,
    validate_http_url_and_resolve_ip_async,
)


SNS_SUBSCRIPTION_TIMEOUT_SECONDS = 10
LOCALSTACK_HOSTNAMES = frozenset(
    {
        "localstack",
        "localhost.localstack.cloud",
    }
)


async def confirm_sns_subscription(subscribe_url: str) -> dict[str, str]:
    rewritten_url = _rewrite_localstack_subscribe_url(subscribe_url)
    validation = await _validate_sns_confirmation_url(rewritten_url)
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
            url=rewritten_url,
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


async def _validate_sns_confirmation_url(url: str) -> HTTPURLValidationResult:
    """Validate the SNS confirmation URL, allowing configured LocalStack endpoints."""
    if _is_configured_localstack_confirmation_url(url):
        return await _resolve_configured_localstack_confirmation_url(url)

    return await validate_http_url_and_resolve_ip_async(url)


async def _resolve_configured_localstack_confirmation_url(
    url: str,
) -> HTTPURLValidationResult:
    parsed_url = urlparse(url)
    hostname = parsed_url.hostname
    if not hostname:
        return HTTPURLValidationResult(
            is_valid=False,
            url=url,
            error_message="URL must include a hostname",
            failure_reason="missing_hostname",
        )

    try:
        loop = asyncio.get_running_loop()
        address_infos = await loop.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        return HTTPURLValidationResult(
            is_valid=False,
            url=url,
            hostname=hostname,
            error_message=f"Unable to resolve hostname {hostname}: {exc}",
            failure_reason="hostname_resolution_failed",
        )

    validated_ip = _extract_first_resolved_ip(address_infos)
    if not validated_ip:
        return HTTPURLValidationResult(
            is_valid=False,
            url=url,
            hostname=hostname,
            error_message=f"Unable to resolve hostname {hostname}",
            failure_reason="hostname_resolution_failed",
        )

    return HTTPURLValidationResult(
        is_valid=True,
        url=url,
        hostname=hostname,
        validated_ip=validated_ip,
    )


def _rewrite_localstack_subscribe_url(subscribe_url: str) -> str:
    parsed_subscribe_url = urlparse(subscribe_url)
    storage_endpoint = _parse_configured_storage_endpoint()
    if not storage_endpoint:
        return subscribe_url

    if not _is_confirm_subscription_action(parsed_subscribe_url):
        return subscribe_url

    if not _is_localstack_endpoint(parsed_subscribe_url):
        return subscribe_url

    if not _is_localstack_endpoint(storage_endpoint):
        return subscribe_url

    return urlunparse(
        parsed_subscribe_url._replace(
            scheme=storage_endpoint.scheme,
            netloc=storage_endpoint.netloc,
        )
    )


def _is_configured_localstack_confirmation_url(url: str) -> bool:
    parsed_url = urlparse(url)
    storage_endpoint = _parse_configured_storage_endpoint()
    if not storage_endpoint:
        return False

    if not _is_confirm_subscription_action(parsed_url):
        return False

    if not _is_localstack_endpoint(parsed_url):
        return False

    if not _is_localstack_endpoint(storage_endpoint):
        return False

    return _endpoint_origin(parsed_url) == _endpoint_origin(storage_endpoint)


def _parse_configured_storage_endpoint() -> ParseResult | None:
    endpoint_url = settings.S3_ENDPOINT_URL.strip()
    if not endpoint_url:
        return None

    parsed_endpoint = urlparse(endpoint_url)
    if parsed_endpoint.scheme not in {"http", "https"}:
        return None
    if not parsed_endpoint.hostname:
        return None
    return parsed_endpoint


def _is_confirm_subscription_action(parsed_url: ParseResult) -> bool:
    actions = parse_qs(parsed_url.query).get("Action", [])
    return any(action.lower() == "confirmsubscription" for action in actions)


def _is_localstack_endpoint(parsed_url: ParseResult) -> bool:
    hostname = (parsed_url.hostname or "").rstrip(".").lower()
    return hostname in LOCALSTACK_HOSTNAMES


def _endpoint_origin(parsed_url: ParseResult) -> tuple[str, str, int | None]:
    return (
        parsed_url.scheme.lower(),
        (parsed_url.hostname or "").rstrip(".").lower(),
        parsed_url.port,
    )


def _extract_first_resolved_ip(address_infos: Sequence[object]) -> str | None:
    for address_info in address_infos:
        if not isinstance(address_info, tuple) or len(address_info) < 5:
            continue
        family = address_info[0]
        socket_address = address_info[4]
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        if not isinstance(socket_address, tuple) or not socket_address:
            continue
        address = socket_address[0]
        if isinstance(address, str) and address:
            return address
    return None
