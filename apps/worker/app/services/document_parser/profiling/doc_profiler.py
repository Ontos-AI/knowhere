"""Parser-entry document profiling."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from loguru import logger
from app.services.document_agent.coordinator import ProfileCoordinator
from app.services.document_parser.orchestration.oversized_pdf_policy import (
    build_oversized_pdf_processing_failed_exception,
    raise_if_oversized_pdf_not_supported,
)
from app.services.document_parser.profiling.profile_model import (
    ParserDocumentProfile,
    ParserTocProfile,
    TocEvidence,
)
from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory

from shared.core.config import settings
from shared.core.database_sync import get_sync_session_factory


def profile_document(
    file_path: str,
    filename: str = "",
    *,
    job_id: str | None = None,
    output_dir: str | None = None,
) -> ParserDocumentProfile:
    """
    General document profiling entry point.

    Args:
        file_path: Local file path
        filename: File name (used to infer type)
        job_id: Parse job id for profile trace artifacts
        output_dir: Parser output directory

    Returns:
        ParserDocumentProfile
    """
    if not filename:
        filename = os.path.basename(file_path)

    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        return _profile_pdf(file_path, filename, job_id=job_id, output_dir=output_dir)

    return ParserDocumentProfile(
        file_type=ext.lstrip("."),
        category=f"{ext.lstrip('.') or 'unknown'} document",
        routing_category=PdfRoutingCategory.GENERIC,
        reasoning=f"Non-PDF format ({ext}), using default route",
    )


def _profile_pdf(
    file_path: str,
    filename: str,
    *,
    job_id: str | None,
    output_dir: str | None,
) -> ParserDocumentProfile:
    with _profile_db_context(enabled=bool(job_id)) as db:
        return _profile_pdf_with_db(
            file_path=file_path,
            filename=filename,
            job_id=job_id,
            output_dir=output_dir,
            db=db,
        )


def _profile_pdf_with_db(
    *,
    file_path: str,
    filename: str,
    job_id: str | None,
    output_dir: str | None,
    db: Any | None,
) -> ParserDocumentProfile:
    profile_job_id = job_id or filename
    agent_output_dir = os.path.join(output_dir, "_doc_agent") if output_dir else None
    page_toc_enabled = settings.PDF_PAGE_TOC_ENABLED
    coordinator = ProfileCoordinator(
        pdf_path=file_path,
        job_id=profile_job_id,
        output_dir=agent_output_dir,
        db=db,
        model=settings.IMAGE_MODEL,
        settings={
            "planner_model": settings.IMAGE_MODEL,
            "vlm_model": settings.IMAGE_MODEL,
            "model": settings.HIERARCHY_LLM_MODEL or settings.NORMOL_MODEL,
            "toc_profile_enabled": page_toc_enabled,
            "toc_before_coarse": page_toc_enabled,
        },
    )
    agent_profile = coordinator.run_coarse()
    routing_category = PdfRoutingCategory.normalize(agent_profile.routing_category)
    profile = ParserDocumentProfile(
        file_type="pdf",
        category=agent_profile.category,
        routing_category=routing_category,
        is_scanned=agent_profile.is_scanned,
        page_count=coordinator.blackboard.page_count,
        language=agent_profile.language,
        reasoning=agent_profile.rationale,
        category_rationale=agent_profile.category_rationale,
        metrics={
            "doc_stats": coordinator.blackboard.doc_stats,
            "doc_shape": coordinator.blackboard.global_signals.get("doc_shape", {}),
            "page_kind_counts": coordinator.blackboard.global_signals.get(
                "page_kind_counts",
                {},
            ),
        },
    )
    if profile.page_count > settings.MAX_PDF_PAGE_LIMIT:
        raise_if_oversized_pdf_not_supported(page_count=profile.page_count)
        if not profile.is_atlas:
            try:
                profile.anatomy = coordinator.run_structural()
                profile.toc = _map_toc_profile(coordinator)
            except Exception as exc:
                raise build_oversized_pdf_processing_failed_exception(
                    page_count=profile.page_count,
                    original_exception=exc,
                ) from exc
        else:
            profile.toc = _map_toc_profile(coordinator)
    else:
        if not profile.is_atlas:
            profile.anatomy = coordinator.run_lightweight_anatomy()
        profile.toc = _map_toc_profile(coordinator)

    if trace := getattr(coordinator, "trace", None):
        trace.persist_doc_profile(profile)

    return profile


def _map_toc_profile(coordinator: ProfileCoordinator) -> ParserTocProfile:
    toc_result = coordinator.blackboard.toc_result
    if toc_result is None:
        return ParserTocProfile()
    attempted_signal = coordinator.blackboard.global_signals.get(
        "toc_profile_attempted"
    )
    attempted = bool(attempted_signal) if attempted_signal is not None else True
    evidence = [
        TocEvidence(
            page_index=item.page_index,
            source=item.source,
            confidence=item.confidence,
            reason=item.reason,
        )
        for item in toc_result.evidence
    ]
    source = "pdf_vlm" if toc_result.method != "none" else "none"
    return ParserTocProfile(
        toc_pages=list(toc_result.toc_pages),
        hierarchies=coordinator.blackboard.toc_hierarchies,
        evidence=evidence,
        source=source,
        method=toc_result.method,
        notes=toc_result.notes,
        attempted=attempted,
    )


@contextmanager
def _profile_db_context(*, enabled: bool) -> Iterator[Any | None]:
    if not enabled:
        yield None
        return
    session = None
    try:
        session = get_sync_session_factory()()
    except Exception as exc:
        logger.debug(f"parse profile db session unavailable: {exc}")
        yield None
        return

    try:
        yield session
        try:
            session.commit()
        except Exception as exc:
            logger.debug(f"parse profile db commit failed: {exc}")
            session.rollback()
    finally:
        session.close()


__all__ = ["profile_document"]
