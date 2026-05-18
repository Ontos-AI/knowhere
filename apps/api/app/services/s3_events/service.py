"""Application service for S3-compatible storage event webhooks."""
from __future__ import annotations

from loguru import logger

from app.services.s3_events.event_handlers import (
    handle_direct_s3_event,
    handle_minio_event,
    handle_oss_event,
    handle_sns_event,
    is_oss_event,
)
from app.services.s3_events.intake_outcome import (
    build_storage_event_error_acknowledgement,
)


async def handle_s3_event_post(
    *,
    body: bytes,
    headers: dict[str, str],
    sns_message_type: str | None,
    minio_auth_token: str | None,
) -> dict[str, str]:
    if sns_message_type:
        result = await handle_sns_event(body)
        if result:
            return result
    elif is_oss_event(headers):
        await handle_oss_event(body, headers)
    elif minio_auth_token:
        await handle_minio_event(body, minio_auth_token)
    else:
        await handle_direct_s3_event(body)

    return {"message": "Event handled successfully"}


async def safely_handle_s3_event_post(
    *,
    body: bytes,
    headers: dict[str, str],
    sns_message_type: str | None,
    minio_auth_token: str | None,
) -> dict[str, str]:
    try:
        return await handle_s3_event_post(
            body=body,
            headers=headers,
            sns_message_type=sns_message_type,
            minio_auth_token=minio_auth_token,
        )
    except Exception as exc:
        outcome = build_storage_event_error_acknowledgement()
        logger.error(f"Failed to handle S3 event: {exc}")
        logger.warning(f"S3 event intake outcome: reason={outcome.reason}")
        return outcome.to_response()
