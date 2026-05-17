from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.services.document_ingestion.processing_context import ParseJobContext
from loguru import logger
from sqlalchemy import select

from shared.core.database_sync import get_sync_db_context
from shared.core.exceptions.domain_exceptions import (
    InsufficientCreditsException,
    NotFoundException,
)
from shared.models.database.job import Job
from shared.services.billing.work_billing_service import WorkBillingService


@dataclass(frozen=True)
class ParseJobBillingSnapshot:
    billing_amount_micro_dollars: int
    billing_credits: float
    billing_status: str


def charge_parse_job_pages(
    *,
    job_id: str,
    filename: str | None,
    job_user_id: str | None,
    page_count: int,
) -> ParseJobBillingSnapshot:
    if not job_user_id:
        raise NotFoundException(
            resource="JobInfo",
            resource_id="user_id",
            internal_message=f"Missing user_id in job info for job_id={job_id}",
        )

    billing_service = WorkBillingService()
    billing_filename = filename or ""
    billing_status = "skipped"
    billing_amount_micro_dollars = 0
    billing_credits = 0.0

    with get_sync_db_context() as db:
        job_result = db.execute(select(Job).where(Job.job_id == job_id).with_for_update())
        job = job_result.scalar_one_or_none()

        if job and getattr(job, "billing_status", "") == "charged":
            logger.info(f"Job already charged: {job_id}")
            billing_status = "charged"
            billing_amount_micro_dollars = int(job.credits_charged or 0)
            billing_credits = billing_amount_micro_dollars / 1_000_000
        else:
            try:
                billing_result = billing_service.charge_for_pages(
                    session=db,
                    user_id=job_user_id,
                    page_count=page_count,
                    filename=billing_filename,
                )
            except InsufficientCreditsException:
                logger.warning(f"Billing failed: job_id={job_id}, user_id={job_user_id}")
                billing_amount = billing_service.estimate_page_charge(
                    page_count=page_count
                )
                if job:
                    job.page_count = page_count
                    job.credits_charged = billing_amount.amount_micro_dollars
                    job.billing_status = "billing_failed"
                    db.commit()

                raise InsufficientCreditsException(
                    user_message=(
                        "Insufficient credits to process this document "
                        f"({page_count} pages required, cost: "
                        f"{billing_amount.credits})."
                    ),
                    required_credits=billing_amount.credits,
                    internal_message=(
                        f"job_id={job_id}, user_id={job_user_id}, "
                        f"page_count={page_count}"
                    ),
                )

            billing_status = billing_result.billing_status
            billing_amount_micro_dollars = billing_result.amount_micro_dollars
            billing_credits = billing_result.credits
            if job:
                job.page_count = page_count
                job.credits_charged = billing_amount_micro_dollars
                job.billing_status = billing_status

    return ParseJobBillingSnapshot(
        billing_amount_micro_dollars=billing_amount_micro_dollars,
        billing_credits=billing_credits,
        billing_status=billing_status,
    )


def record_processing_start(
    *,
    job_id: str,
    job_context: ParseJobContext,
    billing_snapshot: ParseJobBillingSnapshot,
    page_count: int,
    processing_started_at: datetime,
) -> None:
    metadata_updates = {
        "page_count": page_count,
        "billing_status": billing_snapshot.billing_status,
        "billing_amount_micro_dollars": billing_snapshot.billing_amount_micro_dollars,
        "billing_credits": billing_snapshot.billing_credits,
        "processing_started_at": processing_started_at.isoformat(),
    }
    job_context.metadata_service.update_metadata(job_id, metadata_updates)
    job_context.job_metadata.update(metadata_updates)
