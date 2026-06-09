"""Parser-entry document profiling."""

from __future__ import annotations

import os

from app.services.document_agent.coordinator import ProfileCoordinator
from app.services.document_parser.orchestration.oversized_pdf_policy import (
    build_oversized_pdf_processing_failed_exception,
    raise_if_oversized_pdf_not_supported,
)
from app.services.document_parser.profiling.profile_model import ParserDocumentProfile
from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory

from shared.core.config import settings


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
    profile_job_id = job_id or filename
    agent_output_dir = os.path.join(output_dir, "_doc_agent") if output_dir else None
    coordinator = ProfileCoordinator(
        pdf_path=file_path,
        job_id=profile_job_id,
        output_dir=agent_output_dir,
        model=settings.IMAGE_MODEL,
        settings={
            "planner_model": settings.IMAGE_MODEL,
            "vlm_model": settings.IMAGE_MODEL,
            "model": settings.HIERARCHY_LLM_MODEL or settings.NORMOL_MODEL,
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
            except Exception as exc:
                raise build_oversized_pdf_processing_failed_exception(
                    page_count=profile.page_count,
                    original_exception=exc,
                ) from exc

    return profile


__all__ = ["profile_document"]
