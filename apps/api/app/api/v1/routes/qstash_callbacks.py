"""QStash callback endpoints."""

from __future__ import annotations

from app.services import qstash_callback_service
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

    return qstash_callback_service.handle_qstash_success_callback(raw_body)


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

    return qstash_callback_service.handle_qstash_failure_callback(raw_body)
