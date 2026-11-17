from __future__ import annotations

from typing import Any

from shared.core.exceptions.domain_exceptions import ValidationException
from shared.models.schemas.job_metadata import JobMetadataHelper


def build_page_memory_page_limit_rejection(
    *,
    job_metadata: dict[str, Any],
    page_count: int,
) -> ValidationException | None:
    """Reject a page-memory document over its page limit before it is charged."""
    if JobMetadataHelper.get_parse_track(job_metadata) != "page_memory":
        return None
    max_pages = JobMetadataHelper.get_page_memory_config(job_metadata).max_pages
    if page_count <= max_pages:
        return None
    return ValidationException(
        user_message=(
            f"Document too large: {page_count} pages exceeds the "
            f"{max_pages}-page limit for page-based processing."
        ),
        violations=[
            {
                "field": "page_count",
                "description": f"Document has {page_count} pages, limit is {max_pages}",
            }
        ],
    )
