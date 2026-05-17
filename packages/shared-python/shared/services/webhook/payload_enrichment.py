"""Delivery-time webhook payload enrichment."""

from collections.abc import Mapping
from typing import Any, cast

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from shared.core.database import get_db_context
from shared.models.database.job import Job
from shared.models.database.webhook import WebhookEvent
from shared.services.jobs.result_delivery import JobResultDeliveryResolver


class WebhookPayloadEnricher:
    """Add fresh Job Result delivery metadata to webhook payloads."""

    def __init__(self, resolver: JobResultDeliveryResolver | None = None) -> None:
        self._resolver = resolver or JobResultDeliveryResolver()

    async def enrich(self, event: WebhookEvent) -> dict[str, Any]:
        payload = dict(cast(Mapping[str, Any], event.payload))

        if payload.get("event") != "job.completed":
            return payload

        try:
            async with get_db_context() as db:
                result = await db.execute(
                    select(Job)
                    .options(selectinload(Job.job_result))
                    .where(Job.job_id == event.job_id)
                )
                job = result.scalar_one_or_none()

                if not job or not job.job_result:
                    logger.warning(
                        f"Job or result not found for enrichment: job_id={event.job_id}"
                    )
                    return payload

                return self._resolver.enrich_payload(
                    payload,
                    job_result=job.job_result,
                )

        except Exception as error:
            logger.error(f"Failed to enrich payload for event {event.id}: {error}")
            return payload
