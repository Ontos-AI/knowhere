from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Literal, cast

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.core.state_machine.service_sync import SyncStateMachineService
from shared.models.database.job import Job
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.models.schemas.progressive_ingestion import StoredSnapshot
from shared.services.jobs.progressive_ingestion import initialize_baseline_state, write_progressive_state
from shared.services.jobs.lifecycle.post_commit_effects import PostCommitEffectPlan
from shared.services.jobs.lifecycle.publication import SyncJobPublicationFinalizer
from shared.services.jobs.lifecycle.publication_trace import PublicationTrace
from shared.services.jobs.lifecycle.result_writer import SyncJobResultWriter
from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox
from shared.utils.json_utils import remove_nul_characters


@dataclass(frozen=True)
class JobSuccessResponse:
    status: Literal["success", "failed"]
    job_id: str
    stored_count: int | None = None
    reason: str | None = None

    @classmethod
    def completed(cls, *, job_id: str, stored_count: int) -> JobSuccessResponse:
        return cls(status="success", job_id=job_id, stored_count=stored_count)

    @classmethod
    def state_transition_failed(cls, *, job_id: str) -> JobSuccessResponse:
        return cls(status="failed", job_id=job_id, reason="state_transition_failed")

    def should_commit(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        response: dict[str, Any] = {
            "status": self.status,
            "job_id": self.job_id,
        }
        if self.stored_count is not None:
            response["stored_count"] = self.stored_count
        if self.reason:
            response["reason"] = self.reason
        return response


@dataclass(frozen=True)
class JobSuccessFinalization:
    response: JobSuccessResponse
    post_commit_effects: PostCommitEffectPlan


class SyncJobSuccessFinalizer:
    """Finalize successful Jobs inside the lifecycle transaction."""

    def __init__(
        self,
        *,
        state_machine: SyncStateMachineService | None = None,
        result_writer: SyncJobResultWriter | None = None,
        publication_finalizer: SyncJobPublicationFinalizer | None = None,
        webhook_outbox: SyncJobWebhookOutbox | None = None,
    ) -> None:
        self._state_machine = state_machine or SyncStateMachineService()
        self._result_writer = result_writer or SyncJobResultWriter()
        self._publication_finalizer = (
            publication_finalizer or SyncJobPublicationFinalizer()
        )
        self._webhook_outbox = webhook_outbox or SyncJobWebhookOutbox()

    def finalize(
        self,
        db: Session,
        *,
        job_id: str,
        result_s3_key: str,
        checksum: str,
        zip_size: int,
        chunks: list[dict[str, Any]],
        stored_count: int,
        delivery_mode: str,
        section_summaries: dict[str, str] | None,
        document_top_summary: str | None = None,
        trace: PublicationTrace | None = None,
    ) -> JobSuccessFinalization:
        safe_chunks = cast(
            list[dict[str, Any]], remove_nul_characters(chunks)
        )
        safe_section_summaries = cast(
            dict[str, str] | None,
            remove_nul_characters(section_summaries),
        )
        safe_document_top_summary = cast(
            str | None,
            remove_nul_characters(document_top_summary),
        )
        with trace.stage("result_binding") if trace is not None else nullcontext():
            job_result = self._result_writer.upsert_job_result(
                db,
                job_id,
                delivery_mode,
                inline_payload={"checksum": checksum},
                result_s3_key=result_s3_key,
                result_size=zip_size,
            )
            if trace is not None:
                trace.bind_result_id(str(job_result.id))
            self._result_writer.replace_chunks(db, job_result.id, safe_chunks)
        publication_outcome = self._publication_finalizer.publish_result(
            db,
            job_id=job_id,
            job_result_id=job_result.id,
            chunks=safe_chunks,
            section_summaries=safe_section_summaries,
            document_top_summary=safe_document_top_summary,
            trace=trace,
        )
        published_document_id = (
            publication_outcome.published_document_state.document_id
            if publication_outcome.published_document_state is not None
            else None
        )
        job_row = db.execute(
            select(Job.job_metadata).where(Job.job_id == job_id)
        ).scalar_one_or_none()
        job_metadata = job_row if isinstance(job_row, dict) else {}
        should_enqueue_structure = bool(
            published_document_id
            and JobMetadataHelper.get_parse_track(job_metadata) == "page_memory"
            and JobMetadataHelper.get_ingestion_mode(job_metadata) == "progressive"
            and str(job_metadata.get("enrichment_status") or "").strip().lower()
            == "text"
        )
        baseline_snapshot: StoredSnapshot | None = None
        if should_enqueue_structure:
            snapshot_id = str(job_metadata.get("progressive_baseline_snapshot_id") or f"baseline_{checksum}")
            baseline_snapshot = StoredSnapshot(
                snapshot_id=snapshot_id, stage="baseline", job_id=job_id,
                job_result_id=str(job_result.id), document_id=str(published_document_id),
                checksum=checksum, archive_key=result_s3_key,
            )
            raw_pages = job_metadata.get("progressive_missing_embedding_pages", [])
            missing_pages = [page for page in raw_pages if isinstance(page, int) and not isinstance(page, bool)] if isinstance(raw_pages, list) else []
            job_result.document_metadata = write_progressive_state(
                job_result.document_metadata,
                initialize_baseline_state(snapshot=baseline_snapshot, missing_pages=missing_pages),
            )

        transition_outcome = self._state_machine.mark_completed_outcome(
            db,
            job_id,
            result_metadata={
                "storage_completed": True,
                "stored_count": stored_count,
                "delivery_mode": delivery_mode,
            },
        )
        if not transition_outcome.succeeded:
            logger.error(
                f"Job {job_id} mark_completed transition failed: "
                f"reason={transition_outcome.reason}"
            )
            return JobSuccessFinalization(
                response=JobSuccessResponse.state_transition_failed(job_id=job_id),
                post_commit_effects=PostCommitEffectPlan.none(),
            )

        webhook_event = self._webhook_outbox.create_event(
            db,
            job_id=job_id,
            event_type="job.completed",
            extra_payload={
                "job_result_id": baseline_snapshot.job_result_id,
                "document_id": baseline_snapshot.document_id,
                "snapshot_id": baseline_snapshot.snapshot_id,
                "checksum": baseline_snapshot.checksum,
            } if baseline_snapshot else None,
        )
        return JobSuccessFinalization(
            response=JobSuccessResponse.completed(
                job_id=job_id,
                stored_count=stored_count,
            ),
            post_commit_effects=PostCommitEffectPlan.from_success(
                cache_invalidation=publication_outcome.cache_invalidation,
                webhook_event_id=webhook_event.event_id if webhook_event else None,
                structure_job_result_id=(
                    str(job_result.id) if should_enqueue_structure else None
                ),
            ),
        )
