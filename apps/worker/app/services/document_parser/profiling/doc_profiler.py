"""Page-memory PDF profiling."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Literal

from loguru import logger
from app.services.document_agent.coordinator import ProfileCoordinator
from app.services.document_agent.pdf_text import page_body_content_map, page_content_map
from app.services.document_agent.visual import purge_debug_visual_dirs, visual_debug_enabled
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
    stop_after: Literal["native_baseline"] | None = None,
    presentation_source: bool = False,
) -> ParserDocumentProfile:
    """
    Profile a page-memory PDF: coarse classification, then TOC or slide structure.

    Args:
        file_path: Local PDF path
        filename: PDF file name (the trace job id when ``job_id`` is unset)
        job_id: Parse job id for profile trace artifacts
        output_dir: Parser output directory
        stop_after: Internal profiling boundary. ``native_baseline`` defers
            detailed assets only after native classification and a complete
            text scan; scanned or empty-text profiles still probe assets.
        presentation_source: The PDF was converted from a presentation
            file, so its pages are slides whatever the page images look like.

    Returns:
        ParserDocumentProfile
    """
    if not filename:
        filename = os.path.basename(file_path)
    try:
        with _profile_db_context(enabled=bool(job_id)) as db:
            return _profile_pdf_with_db(
                file_path=file_path,
                filename=filename,
                job_id=job_id,
                output_dir=output_dir,
                db=db,
                stop_after=stop_after,
                presentation_source=presentation_source,
            )
    finally:
        if not visual_debug_enabled():
            purge_debug_visual_dirs(output_dir)


def _profile_pdf_with_db(
    *,
    file_path: str,
    filename: str,
    job_id: str | None,
    output_dir: str | None,
    db: Any | None,
    stop_after: Literal["native_baseline"] | None,
    presentation_source: bool,
) -> ParserDocumentProfile:
    profile_job_id = job_id or filename
    agent_output_dir = os.path.join(output_dir, "_doc_agent") if output_dir else None
    coordinator = ProfileCoordinator(
        pdf_path=file_path,
        job_id=profile_job_id,
        output_dir=agent_output_dir,
        db=db,
        settings={
            "vlm_model": settings.IMAGE_MODEL,
            "stop_after_asset_probe": stop_after is not None,
            "stop_after_native_baseline": stop_after is not None,
            "presentation_source": presentation_source,
        },
    )
    agent_profile = coordinator.run_coarse()
    routing_category = PdfRoutingCategory.normalize(agent_profile.routing_category)
    profile = ParserDocumentProfile(
        file_type="pdf",
        category=agent_profile.category,
        category_label=agent_profile.category_label,
        routing_category=routing_category,
        is_scanned=agent_profile.is_scanned,
        page_count=coordinator.blackboard.page_count,
        language=agent_profile.language,
        metrics={
            "doc_stats": coordinator.blackboard.doc_stats,
            "doc_shape": coordinator.blackboard.global_signals.get("doc_shape", {}),
            "page_kind_counts": coordinator.blackboard.global_signals.get(
                "page_kind_counts",
                {},
            ),
        },
        page_full_text_cache=page_content_map(
            coordinator.blackboard.page_full_text_cache
        ),
        page_body_text_cache=page_body_content_map(
            coordinator.blackboard.page_full_text_cache
        ),
    )
    setattr(profile, "blackboard", coordinator.blackboard)
    if stop_after is not None:
        if trace := getattr(coordinator, "trace", None):
            trace.persist_doc_profile(profile)
            setattr(profile, "trace_recorder", trace)
        return profile
    profile.anatomy = coordinator.run_anatomy()
    profile.toc = _map_toc_profile(coordinator)

    if trace := getattr(coordinator, "trace", None):
        trace.persist_doc_profile(profile)
        setattr(profile, "trace_recorder", trace)

    return profile


def _map_toc_profile(coordinator: ProfileCoordinator) -> ParserTocProfile:
    toc_result = coordinator.blackboard.toc_result
    if toc_result is None:
        return ParserTocProfile()
    evidence = [
        TocEvidence(
            page_index=item.page_index,
            source=item.source,
            reason=item.reason,
        )
        for item in toc_result.evidence
    ]
    source = toc_result.profile_source
    return ParserTocProfile(
        toc_pages=list(toc_result.toc_pages),
        hierarchies=coordinator.blackboard.toc_hierarchies,
        evidence=evidence,
        source=source,
        method=toc_result.method,
        notes=toc_result.notes,
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
