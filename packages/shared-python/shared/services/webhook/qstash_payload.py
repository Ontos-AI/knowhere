from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from shared.models.database.job import Job
from shared.services.jobs.result_delivery import JobResultDeliveryResolver


class QStashPayloadEnricher:
    """Sync payload enricher for QStash webhook publication."""

    def __init__(self, resolver: JobResultDeliveryResolver | None = None) -> None:
        self._resolver = resolver or JobResultDeliveryResolver()

    def enrich(self, db: Any, event: Any) -> dict[str, Any]:
        payload = dict(event.payload)
        if payload.get("event") != "job.completed":
            return payload

        try:
            result = db.execute(
                select(Job)
                .options(selectinload(Job.job_result))
                .where(Job.job_id == event.job_id)
            )
            job = result.scalar_one_or_none()
            if not job or not job.job_result:
                return payload

            return self._resolver.enrich_payload(
                payload,
                job_result=job.job_result,
            )
        except Exception as exc:
            logger.error(f"Failed to enrich payload for event {event.id}: {exc}")
            return payload
