"""QStash callback endpoints."""

from __future__ import annotations

from app.services.webhook import qstash_callback_service
from app.services.webhook.qstash_callback_service import QStashCallbackOutcome
from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["QStash Callbacks"])


@router.post("/qstash/callback")
async def handle_qstash_callback(request: Request) -> Response:
    """Handle QStash success callback after webhook delivery."""
    raw_body = await request.body()
    signature = request.headers.get("upstash-signature", "")
    verification_url = qstash_callback_service.get_qstash_verification_url(
        "/webhooks/qstash/callback",
        str(request.url),
    )

    if not qstash_callback_service.verify_qstash_signature(
        raw_body,
        signature,
        verification_url,
    ):
        return Response(status_code=401, content="Invalid signature")

    return _to_response(
        qstash_callback_service.handle_qstash_success_callback(raw_body)
    )


@router.post("/qstash/failure")
async def handle_qstash_failure(request: Request) -> Response:
    """Handle QStash failure callback after all retries exhausted."""
    raw_body = await request.body()
    signature = request.headers.get("upstash-signature", "")
    verification_url = qstash_callback_service.get_qstash_verification_url(
        "/webhooks/qstash/failure",
        str(request.url),
    )

    if not qstash_callback_service.verify_qstash_signature(
        raw_body,
        signature,
        verification_url,
    ):
        return Response(status_code=401, content="Invalid signature")

    return _to_response(
        qstash_callback_service.handle_qstash_failure_callback(raw_body)
    )


def _to_response(outcome: QStashCallbackOutcome) -> Response:
    content_by_kind = {
        "processed": "OK",
        "missing_event_id": "OK (no event_id)",
        "event_not_found": "OK (event not found)",
    }
    return Response(status_code=200, content=content_by_kind[outcome.kind])
