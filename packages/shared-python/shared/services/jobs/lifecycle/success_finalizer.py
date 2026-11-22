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
from shared.services.jobs.lifecycle.scope_enrichments import (
    mark_chunk_final_queued,
    write_coarse_scope_enrichments,
    write_coarse_sheet_group_scopes,
)
from shared.services.jobs.lifecycle.directory_ready import capture_initial_directory
from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox
from shared.services.jobs.progressive_ingestion import (
    initialize_baseline_state,
    initialize_sheet_baseline_state,
    write_progressive_state,
)
from shared.services.jobs.lifecycle.post_commit_effects import PostCommitEffectPlan
from shared.services.jobs.lifecycle.publication_trace import (
    PUBLICATION_PARSE_TRACKS,
    PublicationTrace,
)
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.utils.json_utils import remove_nul_characters


# Job ``enrichment_status`` the worker records on a progressive baseline.
_BASELINE_ENRICHMENT_STATUS_BY_TRACK: dict[str, str] = {
    "page_memory": "text",
    "chunk": "text",
    "media": "text",
    "sheet": "coarse",
}


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

        job_result, job_metadata, user_id = db.execute(
            select(JobResult, Job.job_metadata, Job.user_id)
            .join(Job, Job.job_id == JobResult.job_id)
            .where(JobResult.job_id == job_id)
        ).one()
        parse_track: str = JobMetadataHelper.get_parse_track(job_metadata)
        document_id: str | None = job_result.document_id or job_result.demo_document_id
        if trace is not None:
            trace.bind_result_id(job_result.id)
            if document_id is not None:
                trace.bind_document_id(document_id)
            if parse_track in PUBLICATION_PARSE_TRACKS:
                trace.bind_parse_track(parse_track)
        baseline_enrichment_status = _BASELINE_ENRICHMENT_STATUS_BY_TRACK.get(parse_track)
        namespace: str = JobMetadataHelper.get_namespace(job_metadata) or "default"
        coarse_initial: bool = (
            parse_track == "page_memory"
            and job_result.document_id is not None
            and CorpusStorage.resolve_namespace(namespace).usesAdditivePublication()
        )
        if coarse_initial:
            baseline_enrichment_status = "coarse"
        if (
            document_id is None
            or baseline_enrichment_status is None
            or JobMetadataHelper.get_ingestion_mode(job_metadata) != "progressive"
            or str(job_metadata.get("enrichment_status") or "").strip().lower() != baseline_enrichment_status
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
        if parse_track == "sheet":
            job_result.document_metadata = write_progressive_state(
                job_result.document_metadata,
                initialize_sheet_baseline_state(snapshot=baseline_snapshot),
            )
            write_coarse_sheet_group_scopes(
                db, document_id=document_id, job_result_id=job_result.id
            )
            return finalization
        raw_pages: object = job_metadata.get("progressive_missing_embedding_pages")
        missing_pages: list[int] = (
            [page for page in raw_pages if isinstance(page, int) and not isinstance(page, bool)]
            if parse_track == "page_memory" and isinstance(raw_pages, list)
            else []
        )
        state = initialize_baseline_state(snapshot=baseline_snapshot, missing_pages=missing_pages)
        # A chunk-track baseline without any text, such as an image-only DOCX,
        # returns nothing a read would queue Final for, so Final starts now.
        queue_final: bool = parse_track == "chunk" and not any(
            str(chunk.get("content") or "").strip() for chunk in safe_chunks
        )
        if queue_final:
            mark_chunk_final_queued(state)
        effects = finalization.post_commit_effects
        if coarse_initial:
            state.stages["structure"] = {"status": "complete", "attempts": 1}
            scope = DocumentPublicationScope(
                user_id=user_id,
                namespace=namespace,
                document_id=document_id,
                job_result_id=job_result.id,
                source_file_name=str(job_metadata.get("source_file_name") or ""),
            )
            write_coarse_scope_enrichments(db, document_id=document_id, job_result_id=job_result.id)
            directory = capture_initial_directory(db, scope=scope, job_id=job_id)
            job_result.document_metadata = {
                **dict(job_result.document_metadata or {}),
                "enrichment_status": "coarse",
                "initial_directory": directory.model_dump(mode="json"),
            }
            directory_event = SyncJobWebhookOutbox().create_event(
                db,
                job_id=job_id,
                event_type="job.document.directory_ready",
                extra_payload=directory.model_dump(mode="json", exclude={"sections"}),
            )
            if directory_event is not None:
                effects = replace(
                    effects,
                    webhook_event_ids=(*effects.webhook_event_ids, directory_event.event_id),
                )
        job_result.document_metadata = write_progressive_state(job_result.document_metadata, state)
        return replace(
            finalization,
            post_commit_effects=replace(
                effects,
                structure_job_result_ids=(job_result.id,) if parse_track == "page_memory" and not coarse_initial else (),
                chunk_final_job_result_ids=(job_result.id,) if queue_final else (),
            ),
        )
