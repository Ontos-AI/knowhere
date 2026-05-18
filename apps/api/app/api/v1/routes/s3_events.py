"""S3 event webhook routes."""

from app.services.s3_events.service import safely_handle_s3_event_post
from app.services.s3_events.intake_outcome import sanitize_storage_event_headers
from fastapi import APIRouter, Header, Request
from loguru import logger

from shared.core.logging import LogEvent

router = APIRouter(tags=["Internal"])


@router.get("/s3-events", response_model=dict, summary="Handle S3 webhook GET requests")
async def handle_s3_events_get(
    request: Request,
    x_amz_sns_message_type: str = Header(None, alias="x-amz-sns-message-type"),
    x_minio_auth_token: str = Header(None, alias="x-minio-auth-token"),
    authorization: str = Header(None),
) -> dict[str, str]:
    """Handle S3-event GET requests, primarily for SNS subscription confirmation."""
    logger.info("======== S3 event GET request ========")
    logger.info(f"Headers: {dict(request.headers)}")
    if request.client:
        logger.info(f"Client IP: {request.client.host}")

    if x_amz_sns_message_type == "SubscriptionConfirmation":
        logger.info("Received an SNS subscription confirmation request")
        return {"message": "SNS subscription confirmed"}

    return {"message": "GET request handled"}


@router.post(
    "/s3-events", response_model=dict, summary="Handle S3 webhook POST requests"
)
async def handle_s3_events(
    request: Request,
    x_amz_sns_message_type: str = Header(None, alias="x-amz-sns-message-type"),
    x_minio_auth_token: str = Header(None, alias="x-minio-auth-token"),
    authorization: str = Header(None),
) -> dict[str, str]:
    """Handle S3-event POST requests from AWS SNS, MinIO, OSS, or tests."""
    logger.bind(event=LogEvent.S3_WEBHOOK_EVENT).info(
        f"S3 event Headers: {sanitize_storage_event_headers(dict(request.headers))}"
    )
    if request.client:
        logger.info(f"Client IP: {request.client.host}")

    return await safely_handle_s3_event_post(
        body=await request.body(),
        headers=dict(request.headers),
        sns_message_type=x_amz_sns_message_type,
        minio_auth_token=x_minio_auth_token,
    )
