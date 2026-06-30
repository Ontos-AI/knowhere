from __future__ import annotations

from shared.core.config import settings
from shared.core.exceptions.domain_exceptions import (
    PDFParsingException,
    ValidationException,
    Violation,
)


def build_oversized_pdf_rejection(
    *,
    file_extension: str,
    page_count: int,
) -> ValidationException | None:
    """Return the pre-parse oversized PDF rejection, if this file must be blocked."""
    if file_extension.lower() != ".pdf":
        return None
    if page_count <= settings.MAX_PDF_PAGE_LIMIT:
        return None
    if not settings.OVERSIZED_PDF_SHARD_ENABLED:
        return _build_standard_page_limit_exception(page_count)
    if page_count > settings.OVERSIZED_PDF_SOFT_LIMIT:
        return _build_soft_limit_exception(page_count)
    return None


def raise_if_oversized_pdf_not_supported(*, page_count: int) -> None:
    rejection = build_oversized_pdf_rejection(
        file_extension=".pdf",
        page_count=page_count,
    )
    if rejection is not None:
        raise rejection


def build_oversized_pdf_processing_failed_exception(
    *,
    page_count: int,
    original_exception: Exception,
) -> PDFParsingException:
    reason = _format_original_error(original_exception)
    page_limit = settings.MAX_PDF_PAGE_LIMIT
    user_message = (
        "Oversized PDF processing failed during sharding: "
        f"{reason}. This document has {page_count} pages and exceeds the "
        f"{page_limit}-page direct processing limit, so it cannot be processed "
        "without a successful shard pipeline."
    )
    return PDFParsingException(
        user_message=user_message,
        reason="OVERSIZED_SHARD_PIPELINE_FAILED",
        internal_message=(
            "Oversized PDF shard pipeline failed; falling back to page-limit "
            f"rejection. page_count={page_count}, limit={page_limit}, "
            f"original={type(original_exception).__name__}: {original_exception}"
        ),
        original_exception=original_exception,
    )


def build_oversized_pdf_profile_failed_exception(
    *,
    page_count: int,
    original_exception: Exception,
) -> PDFParsingException:
    reason = _format_original_error(original_exception)
    return PDFParsingException(
        user_message=(
            "Oversized PDF page-memory profiling failed: "
            f"{reason}. This document has {page_count} pages and requires a "
            "successful structural profile before page-memory parsing can continue."
        ),
        reason="OVERSIZED_PAGE_MEMORY_PROFILE_FAILED",
        internal_message=(
            "Oversized PDF page-memory structural profile failed. "
            f"page_count={page_count}, "
            f"original={type(original_exception).__name__}: {original_exception}"
        ),
        original_exception=original_exception,
    )


def _build_standard_page_limit_exception(page_count: int) -> ValidationException:
    page_limit = settings.MAX_PDF_PAGE_LIMIT
    return ValidationException(
        user_message=(
            f"Document too large: {page_count} pages exceeds the {page_limit}-page "
            "limit. Please split the document and upload it in smaller parts."
        ),
        violations=[_page_limit_violation(page_count, page_limit)],
    )


def _build_soft_limit_exception(page_count: int) -> ValidationException:
    soft_limit = settings.OVERSIZED_PDF_SOFT_LIMIT
    return ValidationException(
        user_message=(
            f"This document has {page_count} pages. Processing ultra-long documents "
            f"over {soft_limit} pages requires dedicated resources. Please contact "
            "support for assistance."
        ),
        violations=[{
            "field": "page_count",
            "description": f"PDF has {page_count} pages, soft limit is {soft_limit}",
        }],
    )


def _page_limit_violation(page_count: int, page_limit: int) -> Violation:
    return {
        "field": "page_count",
        "description": f"PDF has {page_count} pages, limit is {page_limit}",
    }


def _format_original_error(exc: Exception) -> str:
    user_message = getattr(exc, "user_message", None)
    if isinstance(user_message, str) and user_message.strip():
        return user_message.strip()
    message = str(exc).strip()
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__
