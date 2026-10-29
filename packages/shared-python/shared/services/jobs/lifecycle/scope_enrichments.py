from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from loguru import logger
from sqlalchemy import ColumnElement, and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from shared.core.config import app_config
from shared.core.database_sync import get_sync_db_context
from shared.models.database.document import (
    Document,
    DocumentChunk,
    DocumentScopeEnrichment,
)
from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.services.jobs.progressive_ingestion import read_progressive_state
from shared.services.retrieval.hydration.row_utils import (
    extract_matched_page_nums,
    extract_page_nums,
    normalize_chunk_type,
    page_enrichment_state,
)
from shared.utils.utc_now import utc_now_naive

_SCOPE_ID_PAGE_RANGE = re.compile(r"^p([1-9]\d*)-([1-9]\d*)$")
_COARSE_STATE = "coarse"
_QUEUED_STATE = "queued"
_RUNNING_STATE = "running"
_FAILED_STATE = "failed"
_RESETTABLE_STATES = (_COARSE_STATE, _QUEUED_STATE)
MAX_SCOPE_ENRICHMENT_ATTEMPTS = 3

ENRICH_SCOPE_TASK_NAME = "app.core.tasks.enrichment_tasks.enrich_page_memory_scope"
BUILD_STRUCTURE_TASK_NAME = (
    "app.core.tasks.enrichment_tasks.build_page_memory_structure"
)
LOW_ENRICHMENT_QUEUE = "document_ingestion_low"
NORMAL_ENRICHMENT_QUEUE = "document_ingestion_medium"
HIGH_ENRICHMENT_QUEUE = "document_ingestion_high"
HIGH_ENRICHMENT_TASK_PRIORITY = 9


def scope_claimable_filter(now: datetime) -> ColumnElement[bool]:
    """Single source of truth for which enrichment rows may be claimed or re-queued.

    A row is claimable when it is still ``coarse``/``queued``, when it failed
    with fewer than ``MAX_SCOPE_ENRICHMENT_ATTEMPTS`` attempts, or when it has
    been stuck in ``running`` past the Celery hard time limit (indicating the
    previous worker was killed by OOM/SIGKILL without releasing the row).
    ``TASK_TIME_LIMIT_SECONDS`` is strictly smaller than the broker redelivery
    window, so a running row older than it cannot still be owned by a live task.
    """
    stale_cutoff = now - timedelta(seconds=app_config.TASK_TIME_LIMIT_SECONDS)
    return and_(
        DocumentScopeEnrichment.attempts < MAX_SCOPE_ENRICHMENT_ATTEMPTS,
        or_(
            DocumentScopeEnrichment.state.in_(_RESETTABLE_STATES),
            and_(
                DocumentScopeEnrichment.state == _FAILED_STATE,
                DocumentScopeEnrichment.attempts < MAX_SCOPE_ENRICHMENT_ATTEMPTS,
            ),
            and_(
                DocumentScopeEnrichment.state == _RUNNING_STATE,
                DocumentScopeEnrichment.updated_at < stale_cutoff,
            ),
        ),
    )


def write_coarse_scope_enrichments(
    db: Session,
    *,
    document_id: str,
    job_result_id: str,
) -> bool:
    """Create ``coarse`` rows in ``document_scope_enrichments`` for published coarse scopes."""
    published = (
        db.execute(
            select(DocumentChunk.chunk_metadata)
            .where(DocumentChunk.document_id == document_id)
            .where(DocumentChunk.job_result_id == job_result_id)
            .where(DocumentChunk.chunk_type == "page")
            .order_by(DocumentChunk.sort_order, DocumentChunk.id)
        )
        .scalars()
        .all()
    )
    scopes: dict[str, tuple[int, int]] = {}
    for metadata in published:
        if not isinstance(metadata, dict):
            continue
        scope_id = str(metadata.get("scope_id") or "").strip()
        if not scope_id or scope_id in scopes:
            continue
        page_range = _page_range_from_scope_id(scope_id)
        if page_range is None:
            continue
        scopes[scope_id] = page_range
    if not scopes:
        return False

    db.execute(
        delete(DocumentScopeEnrichment).where(
            DocumentScopeEnrichment.job_result_id == job_result_id
        )
    )
    now = utc_now_naive()
    db.add_all(
        [
            DocumentScopeEnrichment(
                id=f"dse_{uuid4().hex[:12]}",
                document_id=document_id,
                job_result_id=job_result_id,
                scope_id=scope_id,
                page_start=page_start,
                page_end=page_end,
                state=_COARSE_STATE,
                attempts=0,
                updated_at=now,
            )
            for scope_id, (page_start, page_end) in scopes.items()
        ]
    )
    db.flush()
    return True


def enqueue_page_memory_structure_after_commit(job_result_id: str) -> bool:
    """Enqueue the background M0 -> coarse structure task at normal priority."""
    if not job_result_id:
        return False
    try:
        from shared.core.celery_app import get_celery_app

        get_celery_app().send_task(
            BUILD_STRUCTURE_TASK_NAME,
            args=[job_result_id],
            queue=NORMAL_ENRICHMENT_QUEUE,
        )
        return True
    except Exception as exc:
        logger.warning(
            "Failed to send build_page_memory_structure task after commit "
            f"(ignored): job_result_id={job_result_id}, error={exc}"
        )
        return False


def _revert_queued_scopes_to_coarse(
    *,
    job_result_id: str,
    scope_ids: list[str],
) -> None:
    now = utc_now_naive()
    try:
        with get_sync_db_context() as db:
            db.execute(
                update(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == job_result_id)
                .where(DocumentScopeEnrichment.scope_id.in_(scope_ids))
                .where(DocumentScopeEnrichment.state == _QUEUED_STATE)
                .values(state=_COARSE_STATE, priority=None, updated_at=now)
            )
    except Exception as exc:
        logger.warning(
            f"Failed to revert unsent scopes to coarse for {job_result_id}: {exc}"
        )


def _row_returned_coarse_pages(row: dict[str, Any]) -> bool:
    if normalize_chunk_type(row.get("chunk_type")) != "page":
        return False
    if page_enrichment_state(row) != _COARSE_STATE:
        return False
    if not (extract_matched_page_nums(row) or extract_page_nums(row)):
        return False
    if str(row.get("content") or "").strip():
        return True
    composed = row.get("composed")
    return isinstance(composed, list) and any(composed)


def _on_demand_retryable_scope_filter(now: datetime) -> ColumnElement[bool]:
    stale_cutoff = now - timedelta(seconds=app_config.TASK_TIME_LIMIT_SECONDS)
    return or_(
        and_(
            DocumentScopeEnrichment.state == _COARSE_STATE,
            DocumentScopeEnrichment.last_hit_at.is_not(None),
        ),
        DocumentScopeEnrichment.state == _QUEUED_STATE,
        and_(
            DocumentScopeEnrichment.state == _FAILED_STATE,
            DocumentScopeEnrichment.attempts < MAX_SCOPE_ENRICHMENT_ATTEMPTS,
            DocumentScopeEnrichment.last_hit_at.is_not(None),
        ),
        and_(
            DocumentScopeEnrichment.state == _RUNNING_STATE,
            DocumentScopeEnrichment.updated_at < stale_cutoff,
        ),
    )


async def promote_scopes_on_read(
    db: Any,
    rows: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Queue Fine for coarse page scopes whose pages a READ actually returned."""
    targets: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not _row_returned_coarse_pages(row):
            continue
        metadata = row.get("chunk_metadata") or row.get("metadata") or {}
        scope_id = str(metadata.get("scope_id") or "").strip()
        job_result_id = str(row.get("job_result_id") or "").strip()
        if not scope_id or not job_result_id:
            continue
        key = (job_result_id, scope_id)
        if key in seen:
            continue
        seen.add(key)
        targets.append(key)

    if not targets:
        return []

    promoted: list[tuple[str, str]] = []
    now = utc_now_naive()
    try:
        for job_result_id, scope_id in targets:
            result = await db.execute(
                update(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == job_result_id)
                .where(DocumentScopeEnrichment.scope_id == scope_id)
                .where(DocumentScopeEnrichment.state == _COARSE_STATE)
                .values(
                    state=_QUEUED_STATE,
                    priority="high",
                    last_hit_at=now,
                    updated_at=now,
                )
                .returning(DocumentScopeEnrichment.scope_id)
            )
            if result.scalar_one_or_none() is not None:
                promoted.append((job_result_id, scope_id))
        if promoted:
            await db.commit()
    except Exception as exc:
        logger.warning(
            f"Failed to promote scope enrichment on read (ignored): {exc}"
        )
        return []

    if not promoted:
        return []

    sent: list[tuple[str, str]] = []
    try:
        from shared.core.celery_app import get_celery_app

        celery_app = get_celery_app()
        for job_result_id, scope_id in promoted:
            celery_app.send_task(
                ENRICH_SCOPE_TASK_NAME,
                args=[job_result_id, scope_id],
                queue=HIGH_ENRICHMENT_QUEUE,
                priority=HIGH_ENRICHMENT_TASK_PRIORITY,
            )
            sent.append((job_result_id, scope_id))
    except Exception as exc:
        logger.warning(
            f"Failed to enqueue promoted scope enrichment on read (reverting unsent to coarse): {exc}"
        )
        now_revert = utc_now_naive()
        unsent_pairs = [item for item in promoted if item not in sent]
        try:
            for job_result_id, scope_id in unsent_pairs:
                await db.execute(
                    update(DocumentScopeEnrichment)
                    .where(DocumentScopeEnrichment.job_result_id == job_result_id)
                    .where(DocumentScopeEnrichment.scope_id == scope_id)
                    .where(DocumentScopeEnrichment.state == _QUEUED_STATE)
                    .values(state=_COARSE_STATE, priority=None, updated_at=now_revert)
                )
            if unsent_pairs:
                await db.commit()
        except Exception as revert_exc:
            logger.warning(
                f"Failed to revert unsent promoted scopes to coarse: {revert_exc}"
            )
        return sent
    return sent


def _should_skip_structure_recovery(
    *,
    doc_meta: object,
    job_meta: object,
    now: datetime,
) -> bool:
    doc_dict = doc_meta if isinstance(doc_meta, dict) else {}
    job_dict = job_meta if isinstance(job_meta, dict) else {}
    state = read_progressive_state(doc_dict)
    if state is not None and (
        state.stages.get("structure", {}).get("status") == "complete"
        or state.enhancement.status in {"failed", "complete"}
    ):
        return True
    raw_claimed_at = doc_dict.get("structure_claimed_at") or job_dict.get(
        "structure_claimed_at"
    )
    if not raw_claimed_at:
        return False
    try:
        claimed_at = datetime.fromisoformat(
            str(raw_claimed_at).replace("Z", "+00:00")
        ).replace(tzinfo=None)
        return (now - claimed_at) < timedelta(
            seconds=app_config.TASK_TIME_LIMIT_SECONDS
        )
    except Exception:
        return False


def sweep_progressive_enrichments_sync(
    *,
    structure_timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Re-enqueue revisions stuck at ``text`` and retry previously-read Fine scopes."""
    now = utc_now_naive()
    structure_cutoff = now - timedelta(seconds=structure_timeout_seconds)
    retried_scopes: list[tuple[str, str]] = []

    with get_sync_db_context() as db:
        structure_rows = db.execute(
            select(
                JobResult.id,
                JobResult.document_metadata,
                Job.job_metadata,
            )
            .select_from(JobResult)
            .join(
                Document,
                and_(
                    Document.current_job_result_id == JobResult.id,
                    Document.status == "active",
                ),
            )
            .join(Job, Job.job_id == JobResult.job_id)
            .where(Job.updated_at < structure_cutoff)
            .where(
                func.coalesce(
                    JobResult.document_metadata["enrichment_status"].as_string(),
                    Job.job_metadata["enrichment_status"].as_string(),
                )
                == "text"
            )
        ).all()
        reenqueued_structures = [
            str(revision_id)
            for revision_id, doc_meta, job_meta in structure_rows
            if not _should_skip_structure_recovery(
                doc_meta=doc_meta, job_meta=job_meta, now=now
            )
        ]

        db.execute(
            update(DocumentScopeEnrichment)
            .where(DocumentScopeEnrichment.state == _RUNNING_STATE)
            .where(DocumentScopeEnrichment.attempts >= MAX_SCOPE_ENRICHMENT_ATTEMPTS)
            .where(
                DocumentScopeEnrichment.updated_at
                < now - timedelta(seconds=app_config.TASK_TIME_LIMIT_SECONDS)
            )
            .values(state=_FAILED_STATE, claim_generation=None, updated_at=now)
        )

        scope_candidates = db.execute(
            select(
                DocumentScopeEnrichment.job_result_id,
                DocumentScopeEnrichment.scope_id,
            )
            .select_from(DocumentScopeEnrichment)
            .join(
                Document,
                and_(
                    Document.document_id == DocumentScopeEnrichment.document_id,
                    Document.current_job_result_id
                    == DocumentScopeEnrichment.job_result_id,
                    Document.status == "active",
                ),
            )
            .where(_on_demand_retryable_scope_filter(now))
        ).all()
        for rev_id, scope_id in scope_candidates:
            claimed = db.execute(
                update(DocumentScopeEnrichment)
                .where(DocumentScopeEnrichment.job_result_id == str(rev_id))
                .where(DocumentScopeEnrichment.scope_id == str(scope_id))
                .where(_on_demand_retryable_scope_filter(now))
                .values(state=_QUEUED_STATE, priority="high", updated_at=now)
                .returning(DocumentScopeEnrichment.scope_id)
            ).scalar_one_or_none()
            if claimed is not None:
                retried_scopes.append((str(rev_id), str(claimed)))

    with get_sync_db_context() as db:
        recovery_revisions = db.scalars(
            select(JobResult.id)
            .join(Document, Document.current_job_result_id == JobResult.id)
            .where(Document.status == "active")
            .where(
                JobResult.document_metadata["progressive"]["enhancement"]["status"]
                .as_string()
                .in_(("pending", "running"))
            )
            .order_by(JobResult.updated_at, JobResult.id)
            .limit(100)
        ).all()
        if recovery_revisions:
            db.execute(
                update(JobResult)
                .where(JobResult.id.in_(recovery_revisions))
                .values(updated_at=now)
            )
    for revision in recovery_revisions:
        enqueue_progressive_recovery_after_commit(str(revision))

    sent_structures: list[str] = []
    for rev_id in reenqueued_structures:
        if enqueue_page_memory_structure_after_commit(rev_id):
            sent_structures.append(rev_id)

    sent_scopes: list[tuple[str, str]] = []
    if retried_scopes:
        try:
            from shared.core.celery_app import get_celery_app

            celery_app = get_celery_app()
            for rev_id, scope_id in retried_scopes:
                celery_app.send_task(
                    ENRICH_SCOPE_TASK_NAME,
                    args=[rev_id, scope_id],
                    queue=HIGH_ENRICHMENT_QUEUE,
                    priority=HIGH_ENRICHMENT_TASK_PRIORITY,
                )
                sent_scopes.append((rev_id, scope_id))
        except Exception as exc:
            logger.warning(f"Sweeper failed to send scope enrichment tasks: {exc}")
            by_rev: dict[str, list[str]] = {}
            for rev_id, scope_id in retried_scopes:
                if (rev_id, scope_id) not in sent_scopes:
                    by_rev.setdefault(rev_id, []).append(scope_id)
            for rev_id, unsent in by_rev.items():
                _revert_queued_scopes_to_coarse(job_result_id=rev_id, scope_ids=unsent)

    return {
        "status": "success",
        "reenqueued_structures": sent_structures,
        "retried_scopes": sent_scopes,
    }


def _page_range_from_scope_id(scope_id: str) -> tuple[int, int] | None:
    matched = _SCOPE_ID_PAGE_RANGE.fullmatch(scope_id)
    if matched is None:
        return None
    page_start = int(matched.group(1))
    page_end = int(matched.group(2))
    if page_end < page_start:
        return None
    return page_start, page_end


def enqueue_progressive_recovery_after_commit(job_result_id: str) -> bool:
    """Admit recovery dispatch; persisted unfinished stages survive a lost broker send."""
    try:
        from shared.core.celery_app import get_celery_app

        get_celery_app().send_task(
            "app.core.tasks.enrichment_tasks.recover_progressive_revision",
            args=[job_result_id],
            queue=LOW_ENRICHMENT_QUEUE,
        )
        return True
    except Exception as exc:
        logger.warning(
            "Failed to dispatch progressive recovery for {}: {}", job_result_id, exc
        )
        return False
