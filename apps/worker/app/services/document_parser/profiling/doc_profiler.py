"""Parser-entry document profiling."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Literal

from loguru import logger
from app.services.document_agent.coordinator import ProfileCoordinator
from app.services.document_agent.pdf_text import page_body_content_map, page_content_map
from app.services.document_agent.visual import purge_debug_visual_dirs, visual_debug_enabled
from app.services.document_parser.orchestration.oversized_pdf_policy import (
    build_oversized_pdf_profile_failed_exception,
    build_oversized_pdf_processing_failed_exception,
    raise_if_oversized_pdf_not_supported,
)
from app.services.document_parser.profiling.domain_taxonomy import OTHER_CODE
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
    oversized_policy: Literal["chunk", "page_memory"] = "chunk",
    skip_toc_anchoring: bool = False,
    stop_after: Literal["asset_probe"] | None = None,
) -> ParserDocumentProfile:
    """
    General document profiling entry point.

    Args:
        file_path: Local file path
        filename: File name (used to infer type)
        job_id: Parse job id for profile trace artifacts
        output_dir: Parser output directory
        oversized_policy: Controls oversized PDF admission. ``chunk`` applies
            the page-limit / oversized-admission gate; ``page_memory`` continues
            to structural profiling.
        skip_toc_anchoring: When True, TOC find/extract still run, but
            ``run_toc_anchoring`` is skipped. Used by the staged debug path
            (Stage-1 TOC after Stage-0 bootstrap) so calibration stays in
            Stage-2.

    Returns:
        ParserDocumentProfile
    """
    if not filename:
        filename = os.path.basename(file_path)

    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        try:
            return _profile_pdf(
                file_path,
                filename,
                job_id=job_id,
                output_dir=output_dir,
                oversized_policy=oversized_policy,
                skip_toc_anchoring=skip_toc_anchoring,
                stop_after=stop_after,
            )
        finally:
            if not visual_debug_enabled():
                purge_debug_visual_dirs(output_dir)

    return ParserDocumentProfile(
        file_type=ext.lstrip("."),
        category=[OTHER_CODE],
        category_label=f"{ext.lstrip('.') or 'unknown'} document",
        routing_category=PdfRoutingCategory.GENERIC,
    )


def _profile_pdf(
    file_path: str,
    filename: str,
    *,
    job_id: str | None,
    output_dir: str | None,
    oversized_policy: Literal["chunk", "page_memory"] = "chunk",
    skip_toc_anchoring: bool = False,
    stop_after: Literal["asset_probe"] | None = None,
) -> ParserDocumentProfile:
    with _profile_db_context(enabled=bool(job_id)) as db:
        return _profile_pdf_with_db(
            file_path=file_path,
            filename=filename,
            job_id=job_id,
            output_dir=output_dir,
            db=db,
            oversized_policy=oversized_policy,
            skip_toc_anchoring=skip_toc_anchoring,
            stop_after=stop_after,
        )


def _profile_pdf_with_db(
    *,
    file_path: str,
    filename: str,
    job_id: str | None,
    output_dir: str | None,
    db: Any | None,
    oversized_policy: Literal["chunk", "page_memory"] = "chunk",
    skip_toc_anchoring: bool = False,
    stop_after: Literal["asset_probe"] | None = None,
) -> ParserDocumentProfile:
    profile_job_id = job_id or filename
    agent_output_dir = os.path.join(output_dir, "_doc_agent") if output_dir else None
    # PROFILE TOC is default-on. page_memory always forces it on (sections are
    # TOC-anchored) even if PDF_PROFILE_TOC_ENABLED is off.
    page_toc_enabled = (
        oversized_policy == "page_memory" or settings.PDF_PROFILE_TOC_ENABLED
    )
    coordinator = ProfileCoordinator(
        pdf_path=file_path,
        job_id=profile_job_id,
        output_dir=agent_output_dir,
        db=db,
        model=settings.IMAGE_MODEL,
        settings={
            "vlm_model": settings.IMAGE_MODEL,
            "toc_profile_enabled": page_toc_enabled,
            "skip_toc_anchoring": bool(skip_toc_anchoring),
            "stop_after_asset_probe": stop_after == "asset_probe",
            "model": settings.HIERARCHY_LLM_MODEL or settings.NORMOL_MODEL,
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
    if stop_after == "asset_probe":
        if trace := getattr(coordinator, "trace", None):
            trace.persist_doc_profile(profile)
            setattr(profile, "trace_recorder", trace)
        return profile
    if profile.page_count > settings.MAX_PDF_PAGE_LIMIT:
        if oversized_policy != "page_memory":
            raise_if_oversized_pdf_not_supported(page_count=profile.page_count)
        if not profile.is_atlas:
            try:
                profile.anatomy = coordinator.run_structural()
                profile.toc = _map_toc_profile(coordinator)
            except Exception as exc:
                if oversized_policy == "page_memory":
                    raise build_oversized_pdf_profile_failed_exception(
                        page_count=profile.page_count,
                        original_exception=exc,
                    ) from exc
                raise build_oversized_pdf_processing_failed_exception(
                    page_count=profile.page_count,
                    original_exception=exc,
                ) from exc
        else:
            # TODO(page_memory): oversized atlas skips anatomy, so coarse
            # has_asset / page_features never reach page_memory (Root fallback).
            profile.toc = _map_toc_profile(coordinator)
    else:
        # TODO(page_memory): non-oversized atlas skips anatomy; coarse
        # has_asset / page_features never reach page_memory via profile.anatomy.
        if not profile.is_atlas:
            profile.anatomy = coordinator.run_lightweight_anatomy()
        profile.toc = _map_toc_profile(coordinator)

    if trace := getattr(coordinator, "trace", None):
        trace.persist_doc_profile(profile)
        setattr(profile, "trace_recorder", trace)

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
