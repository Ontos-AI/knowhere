"""Progressive-enrichment state queries for ``corpus.*`` tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.database.job import Job
from shared.models.database.job_result import JobResult
from shared.services.retrieval.corpus_storage import CorpusStorage
from shared.services.retrieval.hydration.row_utils import (
    extract_page_nums,
    page_enrichment_state,
)

STATUS_TEXT = "text"
STATUS_COARSE = "coarse"
STATUS_COMPLETE = "complete"
UNREFINED_PAGE_STATES = frozenset({"text", "coarse", "queued"})


@dataclass(frozen=True)
class ScopePageStateSummary:
    states: frozenset[str]
    page_min: int | None
    page_max: int | None
    page_rows: tuple[dict[str, Any], ...]

    @property
    def has_unrefined_pages(self) -> bool:
        return bool(self.states & UNREFINED_PAGE_STATES)

    @property
    def page_range_label(self) -> str:
        if self.page_min is not None and self.page_max is not None:
            if self.page_min == self.page_max:
                return f"page {self.page_min}"
            return f"pages {self.page_min}-{self.page_max}"
        return "the requested scope"


async def load_revision_enrichment_status(
    db: AsyncSession,
    job_result_ids: list[str] | set[str],
) -> dict[str, str]:
    """Return ``job_result_id -> status`` ('text' | 'coarse' | 'complete').

    Absent keys on full-mode or legacy revisions resolve to ``'complete'``.
    """
    ids = sorted({str(item).strip() for item in job_result_ids if str(item).strip()})
    if not ids:
        return {}
    out: dict[str, str] = {rev_id: STATUS_COMPLETE for rev_id in ids}
    rows = (
        await db.execute(
            select(
                JobResult.id,
                JobResult.document_metadata,
                Job.job_metadata,
            )
            .select_from(JobResult)
            .outerjoin(Job, Job.job_id == JobResult.job_id)
            .where(JobResult.id.in_(ids))
        )
    ).all()
    for rev_id, doc_meta, job_meta in rows:
        raw_status = None
        if isinstance(doc_meta, dict):
            raw_status = doc_meta.get("enrichment_status")
        if not raw_status and isinstance(job_meta, dict):
            raw_status = job_meta.get("enrichment_status")
        normalized = str(raw_status or "").strip().lower()
        if normalized in (STATUS_TEXT, STATUS_COARSE, STATUS_COMPLETE):
            out[str(rev_id)] = normalized
        else:
            out[str(rev_id)] = STATUS_COMPLETE
    return out


async def inspect_scope_page_states(
    db: AsyncSession,
    *,
    namespace: str,
    document_id: str,
    job_result_id: str,
    section_path: str | None = None,
) -> ScopePageStateSummary:
    """Inspect page chunks under ``(document_id, job_result_id, section_path?)``."""
    corpus_storage = CorpusStorage.resolve_namespace(namespace)
    stmt = (
        select(
            corpus_storage.DocumentChunk.chunk_id,
            corpus_storage.DocumentChunk.chunk_type,
            corpus_storage.DocumentChunk.chunk_metadata,
            corpus_storage.DocumentSection.section_path,
        )
        .select_from(corpus_storage.DocumentChunk)
        .outerjoin(
            corpus_storage.DocumentSection,
            corpus_storage.DocumentSection.section_id
            == corpus_storage.DocumentChunk.section_id,
        )
        .where(corpus_storage.DocumentChunk.document_id == document_id)
        .where(corpus_storage.DocumentChunk.job_result_id == job_result_id)
        .where(corpus_storage.DocumentChunk.chunk_type == "page")
    )
    if section_path is not None:
        stmt = stmt.where(
            or_(
                corpus_storage.DocumentSection.section_path == section_path,
                corpus_storage.DocumentSection.section_path.like(
                    f"{section_path} / %"
                ),
            )
        )
    rows = (await db.execute(stmt)).all()
    states: set[str] = set()
    page_nums_all: list[int] = []
    page_rows: list[dict[str, Any]] = []
    for chunk_id, chunk_type, metadata, resolved_path in rows:
        meta_dict = metadata if isinstance(metadata, dict) else {}
        row_dict: dict[str, Any] = {
            "document_id": document_id,
            "job_result_id": job_result_id,
            "chunk_id": str(chunk_id or ""),
            "chunk_type": str(chunk_type or "page"),
            "chunk_metadata": meta_dict,
            "section_path": resolved_path,
        }
        state = page_enrichment_state(row_dict)
        if state:
            states.add(state)
        nums = extract_page_nums(row_dict) or []
        for num in nums:
            try:
                page_nums_all.append(int(num))
            except (TypeError, ValueError):
                continue
        page_rows.append(row_dict)
    return ScopePageStateSummary(
        states=frozenset(states),
        page_min=min(page_nums_all) if page_nums_all else None,
        page_max=max(page_nums_all) if page_nums_all else None,
        page_rows=tuple(page_rows),
    )


def format_still_structuring_note(file_names: list[str]) -> str:
    joined = ", ".join(file_names) if file_names else "document"
    return (
        f"{joined} is still being structured (state=text); no section outline yet. "
        "Do not retry outline, node_filter, or assets on it in this session — "
        "use corpus.recall (hybrid text and visual semantic recall), "
        "corpus.grep, and corpus.read on pages directly."
    )


def format_assets_not_refined_note(page_range_label: str, state: str = "coarse") -> str:
    return (
        f"assets for {page_range_label} are not extracted yet (state={state}); "
        "background refinement is queued. Do not retry corpus.assets or "
        "corpus.query_table on this range in this session — use page text "
        "via corpus.read, corpus.grep, or corpus.recall."
    )


def format_scanned_pages_pending_ocr_note(file_names: list[str]) -> str:
    joined = ", ".join(file_names) if file_names else "document"
    return (
        f"{joined} is a scanned document still awaiting background OCR (state=text); "
        "no page text is indexed for exact string search yet. Do not retry "
        "corpus.grep on it in this session — use corpus.recall (visual semantic "
        "search over page images) and corpus.read to inspect page images."
    )
