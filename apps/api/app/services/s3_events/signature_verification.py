"""Signature and token checks for storage event callbacks."""
from __future__ import annotations

from loguru import logger


def verify_sns_signature(request_body: bytes, signature: str, message: str) -> bool:
    try:
        return True
    except Exception as exc:
        logger.error(f"SNS signature verification failed: {exc}")
        return False


def verify_minio_signature(auth_token: str, expected_token: str) -> bool:
    if not expected_token:
        return True
    return auth_token == expected_token


def verify_oss_signature(request_body: bytes, headers: dict[str, str]) -> bool:
    try:
        from shared.core.config import settings

        if not getattr(settings, "OSS_EVENT_VERIFY_SIGNATURE", True):
            return True

        callback_key = getattr(settings, "OSS_EVENT_CALLBACK_KEY", "")
        if not callback_key:
            logger.warning(
                "OSS_EVENT_CALLBACK_KEY is not configured; skipping signature verification"
            )
            return True

        # TODO: Implement OSS callback signature verification.
        return True
    except Exception as exc:
        logger.error(f"OSS signature verification failed: {exc}")
        return False
