"""Claim and settle revision-level progressive enhancement stages."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, noload

from shared.core.config import app_config
from shared.core.database_sync import get_sync_db_context
from shared.models.database.document import Document
from shared.models.database.job_result import JobResult
from shared.models.schemas.progressive_ingestion import (
    EnhancementProgress,
    EnhancementStage,
)
from shared.services.jobs.progressive_ingestion import (
    read_progressive_state,
    write_progressive_state,
)
from shared.services.retrieval.publication_models import DocumentPublicationScope
from shared.utils.utc_now import utc_now_naive

_REQUIRED_STAGES_BEFORE_FINALIZATION: dict[str, tuple[str, ...]] = {
    "page_memory": ("structure", "vector_repair"),
    "sheet": (),
    "media": (),
}


def required_stages_before_finalization(parse_track: str) -> tuple[str, ...]:
    """Revision stages that must be complete before a scope track can finalize."""
    return _REQUIRED_STAGES_BEFORE_FINALIZATION[parse_track]


def claim_progressive_stage(
    *, job_result_id: str, stage_name: str, db: Session | None = None
) -> str | None:
    now: datetime = utc_now_naive()
    with nullcontext(db) if db is not None else get_sync_db_context() as db:
        result = db.execute(
            select(JobResult)
            .options(noload("*"))
            .where(JobResult.id == job_result_id)
            .with_for_update(of=JobResult)
        ).scalar_one_or_none()
        if result is None:
            return None
        state = read_progressive_state(result.document_metadata)
        if state is None or state.enhancement.status in ("complete", "failed"):
            return None
        stages: dict[str, EnhancementStage] = dict(state.stages)
        stage: EnhancementStage = {**stages.get(stage_name, {})}
        if stage.get("status") == "complete":
            return None
        if stage.get("status") == "running" and stage.get("claimed_at"):
            claimed_at = datetime.fromisoformat(str(stage.get("claimed_at"))).replace(
                tzinfo=None
            )
            if now - claimed_at < timedelta(seconds=app_config.TASK_TIME_LIMIT_SECONDS):
                return None
        attempts: int = int(stage.get("attempts", 0))
        if attempts >= 3:
            stages[stage_name] = {**stage, "status": "failed", "claim_generation": None}
            state.stages = stages
            state.enhancement = EnhancementProgress(
                status="failed",
                can_retry=True,
                attempt=state.enhancement.attempt,
                error="Background enhancement exhausted its automatic retries",
            )
            result.document_metadata = write_progressive_state(
                result.document_metadata, state
            )
            return None
        token: str = str(uuid4())
        stages[stage_name] = {
            **stage,
            "status": "running",
            "attempts": attempts + 1,
            "claim_generation": token,
            "claimed_at": now.isoformat(),
            "error": None,
        }
        state.stages = stages
        state.enhancement = EnhancementProgress(
            status="running", attempt=state.enhancement.attempt
        )
        result.document_metadata = write_progressive_state(
            result.document_metadata, state
        )
        return token


def finish_progressive_stage(
    *,
    job_result_id: str,
    stage_name: str,
    claim_generation: str,
    error: str | None,
    pending_pages: list[int] | None = None,
) -> bool:
    with get_sync_db_context() as db:
        document: Document | None = None
        if stage_name == "vector_repair":
            document_id: str | None = db.scalar(
                select(JobResult.document_id).where(JobResult.id == job_result_id)
            )
            if document_id is None:
                return False
            document = db.execute(
                select(Document)
                .where(Document.document_id == document_id)
                .with_for_update()
            ).scalar_one_or_none()
            if (
                document is None
                or document.status != "active"
                or document.current_job_result_id != job_result_id
            ):
                return False
        result = db.execute(
            select(JobResult)
            .options(noload("*"))
            .where(JobResult.id == job_result_id)
            .with_for_update(of=JobResult)
        ).scalar_one_or_none()
        if result is None:
            return False
        if document is not None and result.document_id != document.document_id:
            return False
        state = read_progressive_state(result.document_metadata)
        if state is None:
            return False
        stages: dict[str, EnhancementStage] = dict(state.stages)
        stage: EnhancementStage = {**stages.get(stage_name, {})}
        if (
            stage.get("claim_generation") != claim_generation
            or stage.get("status") != "running"
        ):
            return False
        if document is not None:
            from shared.services.retrieval.page_embedding_index import (
                load_missing_document_page_embedding_pages,
            )

            # Provider results and the initial probe can outlive page owners.
            # Settle every repair exit against coverage under the publication lock.
            pending_pages = load_missing_document_page_embedding_pages(
                db,
                scope=DocumentPublicationScope(
                    user_id=document.user_id,
                    namespace=document.namespace,
                    document_id=document.document_id,
                    job_result_id=job_result_id,
                    source_file_name=document.source_file_name,
                ),
            )
            if pending_pages:
                error = error or "Page embedding provider left missing coverage"
            else:
                error = None
        stage["status"] = "failed" if error else "complete"
        stage["error"] = error
        if pending_pages is not None:
            stage["pending_pages"] = pending_pages
        stages[stage_name] = stage
        state.stages = stages
        if error and int(stage.get("attempts", 0)) >= 3:
            state.enhancement = EnhancementProgress(
                status="failed",
                can_retry=True,
                attempt=state.enhancement.attempt,
                error="Background enhancement exhausted its automatic retries",
            )
        result.document_metadata = write_progressive_state(
            result.document_metadata, state
        )
        return True
