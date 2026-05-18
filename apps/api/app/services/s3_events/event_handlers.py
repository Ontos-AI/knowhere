"""Storage event protocol handlers."""
from __future__ import annotations

import json
import os
from typing import Any

from app.services.s3_events.nested_message_decoder import decode_nested_json_message
from app.services.s3_events.signature_verification import (
    verify_minio_signature,
    verify_oss_signature,
)
from app.services.s3_events.subscription_service import confirm_sns_subscription
from app.services.s3_events.upload_event_service import process_upload_events
from loguru import logger

from shared.models.schemas.oss_event import OSSEvent
from shared.models.schemas.s3_event import S3Event


async def handle_sns_event(body: bytes) -> dict[str, str] | None:
    try:
        sns_message = json.loads(body.decode("utf-8"))
        message_type = sns_message.get("Type")
        logger.info(f"SNS message type: {message_type}")

        if message_type == "SubscriptionConfirmation":
            logger.info("Received an SNS subscription confirmation request")
            subscribe_url = sns_message.get("SubscribeURL")
            if subscribe_url:
                logger.info(f"SNS subscription confirmation URL: {subscribe_url}")
                return await confirm_sns_subscription(subscribe_url)

            logger.warning("SNS subscription confirmation did not include SubscribeURL")
            return {"message": "SNS subscription confirmation failed"}

        if message_type == "Notification":
            logger.info("Received an SNS notification")
            logger.info(f"SNS message payload: {sns_message}")
            await _handle_sns_notification(sns_message)
            return None

        logger.warning(f"Unknown SNS message type: {message_type}")
        return {"message": f"Unknown SNS message type: {message_type}"}

    except Exception as exc:
        logger.error(f"Failed to handle SNS event: {exc}")
        raise


async def handle_minio_event(body: bytes, auth_token: str) -> None:
    try:
        from shared.core.config import settings

        expected_token = getattr(settings, "S3_WEBHOOK_AUTH_TOKEN", "")
        if not verify_minio_signature(auth_token, expected_token):
            logger.warning("MinIO webhook authentication failed")
            return

        s3_event_data = json.loads(body.decode("utf-8"))
        await process_upload_events(S3Event(**s3_event_data))

    except Exception as exc:
        logger.error(f"Failed to handle MinIO event: {exc}")


async def handle_direct_s3_event(body: bytes) -> None:
    try:
        s3_event_data = json.loads(body.decode("utf-8"))
        await process_upload_events(S3Event(**s3_event_data))

    except Exception as exc:
        logger.error(f"Failed to handle direct S3 event: {exc}")


def is_oss_event(headers: dict[str, str]) -> bool:
    storage_type = os.getenv("S3_TYPE", "s3").lower()
    if storage_type == "oss":
        return True

    if "x-oss-pub-key-url" in headers:
        return True

    if "x-mns-version" in headers or "x-mns-signing-cert-url" in headers:
        return True
    user_agent = headers.get("user-agent") or headers.get("User-Agent")
    return bool(user_agent and "Aliyun Notification Service Agent" in user_agent)


async def handle_oss_event(body: bytes, headers: dict[str, str]) -> None:
    try:
        if not verify_oss_signature(body, headers):
            logger.warning("OSS event signature verification failed")
            return

        event_data = json.loads(body.decode("utf-8"))
        logger.info(f"OSS event payload: {event_data}")
        event_data = _unwrap_mns_message(event_data)

        if "events" in event_data:
            oss_event = OSSEvent(**event_data)
        elif "Records" in event_data:
            oss_event = convert_s3_format_to_oss(event_data)
        else:
            logger.error(f"Unknown OSS event format: {event_data}")
            return

        await process_upload_events(oss_event.to_s3_event())

    except Exception as exc:
        logger.error(f"Failed to handle OSS event: {exc}")
        raise


def convert_s3_format_to_oss(event_data: dict[str, Any]) -> OSSEvent:
    from shared.models.schemas.oss_event import OSSEventRecord

    records = event_data.get("Records", [])
    oss_records = [
        OSSEventRecord(
            eventName=record.get("eventName", "").replace("s3:", ""),
            eventSource="acs:oss",
            eventTime=record.get("eventTime", ""),
            region=record.get("awsRegion", ""),
            oss={
                "bucket": record.get("s3", {}).get("bucket", {}),
                "object": record.get("s3", {}).get("object", {}),
            },
        )
        for record in records
    ]

    return OSSEvent(events=oss_records)


async def _handle_sns_notification(sns_message: dict[str, Any]) -> None:
    try:
        s3_event_data = json.loads(sns_message["Message"])
        logger.info(f"S3 event payload: {s3_event_data}")

        if (
            isinstance(s3_event_data, dict)
            and s3_event_data.get("Event") == "s3:TestEvent"
        ):
            logger.info("Skip S3 test event")
            return

        await process_upload_events(S3Event(**s3_event_data))
    except Exception as exc:
        logger.error(f"Failed to parse the S3 event payload: {exc}")
        logger.error(f"SNS payload: {sns_message}")
        try:
            await process_upload_events(S3Event(**sns_message))
        except Exception as fallback_exc:
            logger.error(
                "Fallback parsing of the SNS payload as an S3 event also failed: "
                f"{fallback_exc}"
            )
            raise


def _unwrap_mns_message(event_data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(event_data, dict) or "Message" not in event_data:
        return event_data

    inner = event_data.get("Message")
    if isinstance(inner, dict):
        return inner
    if not isinstance(inner, str):
        return event_data

    decoded = decode_nested_json_message(inner)
    if decoded is not None:
        logger.info(f"Decoded MNS Message payload: {decoded}")
        return decoded
    return event_data
