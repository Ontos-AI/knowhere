from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.models.schemas.job_metadata import JobMetadataHelper
from shared.models.schemas.progressive_ingestion import StoredSnapshot
from shared.services.jobs.progressive_ingestion import initialize_baseline_state, write_progressive_state
from shared.services.jobs.lifecycle.post_commit_effects import PostCommitEffectPlan
from shared.services.jobs.lifecycle.publication_trace import PublicationTrace
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
        parsingOwnerToken: str | None = None,
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
        from shared.services.retrieval.lexical.parsing import ParsedRevisionFinalizer

        with trace.stage("result_binding") if trace is not None else nullcontext():
            finalization = ParsedRevisionFinalizer().finalize(
                db,
                jobId=job_id,
                resultKey=result_s3_key,
                checksum=checksum,
                resultSize=zip_size,
                chunks=safe_chunks,
                storedCount=stored_count,
                deliveryMode=delivery_mode,
                sectionSummaries=safe_section_summaries,
                documentTopSummary=safe_document_top_summary,
                parsingOwnerToken=parsingOwnerToken,
            )
        if not finalization.post_commit_effects.indexing_job_ids:
            return finalization

        job_result, job_metadata = db.execute(
            select(JobResult, Job.job_metadata)
            .join(Job, Job.job_id == JobResult.job_id)
            .where(JobResult.job_id == job_id)
        ).one()
        parse_track: str = JobMetadataHelper.get_parse_track(job_metadata)
        document_id: str | None = job_result.document_id or job_result.demo_document_id
        if trace is not None:
            trace.bind_result_id(job_result.id)
            if document_id is not None:
                trace.bind_document_id(document_id)
            if parse_track in ("chunk", "page_memory"):
                trace.bind_parse_track(parse_track)
        if (
            document_id is None
            or parse_track not in {"page_memory", "chunk"}
            or JobMetadataHelper.get_ingestion_mode(job_metadata) != "progressive"
            or str(job_metadata.get("enrichment_status") or "").strip().lower() != "text"
        ):
            return finalization

        snapshot_id: str = str(
            job_metadata.get("progressive_baseline_snapshot_id") or f"baseline_{checksum}"
        )
        baseline_snapshot = StoredSnapshot(
            snapshot_id=snapshot_id,
            stage="baseline",
            job_id=job_id,
            job_result_id=job_result.id,
            document_id=document_id,
            checksum=checksum,
            archive_key=result_s3_key,
        )
        raw_pages: object = job_metadata.get("progressive_missing_embedding_pages")
        missing_pages: list[int] = (
            [page for page in raw_pages if isinstance(page, int) and not isinstance(page, bool)]
            if parse_track == "page_memory" and isinstance(raw_pages, list)
            else []
        )
        job_result.document_metadata = write_progressive_state(
            job_result.document_metadata,
            initialize_baseline_state(snapshot=baseline_snapshot, missing_pages=missing_pages),
        )
        return replace(
            finalization,
            post_commit_effects=replace(
                finalization.post_commit_effects,
                structure_job_result_ids=(job_result.id,) if parse_track == "page_memory" else (),
                chunk_final_job_result_ids=(),
            ),
        )
