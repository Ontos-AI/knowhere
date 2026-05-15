from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import NAMESPACE_URL, uuid5

from fastapi import Response
from loguru import logger
from sqlalchemy import select

from shared.core.config import app_config
from shared.core.database_sync import get_sync_db_context
from shared.models.database.webhook import WebhookEvent, WebhookEventStatus
from shared.models.database.webhook_log import WebhookLog


def get_qstash_verification_url(callback_path: str, request_url: str) -> str:
    callback_base_url = app_config.QSTASH_CALLBACK_BASE_URL
    if callback_base_url:
        return f"{callback_base_url.rstrip('/')}{callback_path}"
    return request_url


def verify_qstash_signature(raw_body: bytes, signature: str, url: str) -> bool:
    current_key = app_config.QSTASH_CURRENT_SIGNING_KEY
    next_key = app_config.QSTASH_NEXT_SIGNING_KEY

    if not current_key or not next_key:
        logger.error("QStash signing keys not configured — rejecting callback")
        return False

    try:
        from qstash import Receiver

        receiver = Receiver(
            current_signing_key=current_key,
            next_signing_key=next_key,
        )
        receiver.verify(
            body=raw_body.decode("utf-8"),
            signature=signature,
            url=url,
        )
        return True
    except Exception as exc:
        logger.warning(
            "QStash signature verification failed: error_type={error_type}, url={url}",
            error_type=type(exc).__name__,
            url=url,
        )
        return False


def handle_qstash_success_callback(raw_body: bytes) -> Response:
    data = extract_callback_data(raw_body)
    event_id = find_event_id(data)

    if not event_id:
        logger.warning("QStash callback: missing event_id, cannot correlate")
        return Response(status_code=200, content="OK (no event_id)")

    retried = data.get("retried", 0)
    logger.info(
        f"QStash callback: event_id={event_id}, status={data.get('status')}, "
        f"retried={retried}, qstash_message_id={data.get('sourceMessageId')}"
    )

    return process_qstash_callback(
        data,
        event_id,
        get_callback_event_status(data),
        "callback",
    )


def handle_qstash_failure_callback(raw_body: bytes) -> Response:
    data = extract_callback_data(raw_body)
    event_id = find_event_id(data)

    if not event_id:
        logger.warning("QStash failure callback: missing event_id, cannot correlate")
        return Response(status_code=200, content="OK (no event_id)")

    retried = data.get("retried", 0)
    max_retries = data.get("maxRetries", 0)
    logger.warning(
        f"QStash failure: event_id={event_id}, status={data.get('status')}, "
        f"retried={retried}/{max_retries}, qstash_message_id={data.get('sourceMessageId')}"
    )

    return process_qstash_callback(
        data,
        event_id,
        WebhookEventStatus.FAILED,
        "failure",
    )


def extract_callback_data(body: bytes) -> dict[str, Any]:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {"raw": body.decode("utf-8", errors="replace")}


def find_event_id(data: dict[str, Any]) -> Optional[str]:
    source_header = data.get("sourceHeader", {}) or {}
    event_id = normalize_header_value(
        source_header.get("X-Knowhere-Event-Id")
        or source_header.get("x-knowhere-event-id")
    )
    if not event_id:
        for key, value in source_header.items():
            if key.lower() == "x-knowhere-event-id":
                event_id = normalize_header_value(value)
                break
    return event_id


def normalize_header_value(value: Any) -> Optional[str]:
    if isinstance(value, list):
        if not value:
            return None
        first_value = value[0]
        return first_value if isinstance(first_value, str) else str(first_value)

    if isinstance(value, str):
        return value

    if value is None:
        return None

    return str(value)


def build_callback_log_idempotency_key(
    qstash_message_id: Optional[str],
    event_id: str,
) -> str:
    if qstash_message_id:
        return str(uuid5(NAMESPACE_URL, qstash_message_id))
    return event_id


def get_response_status_code(value: Any) -> Optional[int]:
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_success_response_status(status_code: Optional[int]) -> bool:
    return status_code is not None and 200 <= status_code < 300


def get_callback_event_status(data: dict[str, Any]) -> str:
    response_status = get_response_status_code(data.get("status"))
    if is_success_response_status(response_status):
        return WebhookEventStatus.DELIVERED
    return WebhookEventStatus.DELIVERING


def resolve_event_status(current_status: str, callback_status: str) -> str:
    if current_status in (
        WebhookEventStatus.DELIVERED,
        WebhookEventStatus.FAILED,
        WebhookEventStatus.CANCELED,
    ):
        return current_status
    return callback_status


def process_qstash_callback(
    data: dict[str, Any],
    event_id: str,
    callback_status: str,
    log_label: str,
) -> Response:
    response_status_code = get_response_status_code(data.get("status"))
    response_body = data.get("body", "")
    qstash_message_id = data.get("sourceMessageId")
    retried = data.get("retried", 0)
    is_failed_delivery_attempt = (
        callback_status == WebhookEventStatus.FAILED
        or (
            callback_status == WebhookEventStatus.DELIVERING
            and not is_success_response_status(response_status_code)
        )
    )
    error_message = None
    if is_failed_delivery_attempt:
        error_message = data.get("error") or response_body

    with get_sync_db_context() as db:
        event = db.execute(
            select(WebhookEvent).where(WebhookEvent.id == event_id)
        ).scalar_one_or_none()

        if not event:
            logger.warning(f"QStash {log_label}: event {event_id} not found in DB")
            return Response(status_code=200, content="OK (event not found)")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        event_status = resolve_event_status(event.status, callback_status)
        attempt_number = retried + 1
        event.status = event_status
        event.attempts = max(event.attempts, attempt_number)
        event.updated_at = now

        log = WebhookLog(
            job_id=event.job_id,
            event_id=event.id,
            webhook_url=event.target_url,
            attempt_number=attempt_number,
            request_payload=event.payload,
            signature="",
            idempotency_key=build_callback_log_idempotency_key(
                qstash_message_id,
                event.id,
            ),
            response_status_code=response_status_code,
            response_body=response_body[:4096] if response_body else None,
            error_message=str(error_message)[:4096] if error_message else None,
            duration_ms=0,
            qstash_message_id=qstash_message_id,
        )
        db.add(log)
        db.commit()

    return Response(status_code=200, content="OK")
