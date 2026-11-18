"""Page-memory admission: the page limit counts document pages, not embedded media."""

from __future__ import annotations

from app.services.document_ingestion.page_estimator import WorkloadEstimate
from app.services.document_ingestion.page_limit_policy import (
    build_page_memory_page_limit_rejection,
)

_PAGE_MEMORY_METADATA = {
    "parse_track": "page_memory",
    "page_memory_config": {"max_pages": 3},
}


def test_deck_under_slide_limit_passes_despite_embedded_media_pages() -> None:
    estimate = WorkloadEstimate(
        page_count=3 + 30,
        method="pptx_slides",
        embedded_media_durations_ms=(60 * 60 * 1000,),
        embedded_media_page_count=30,
    )

    assert estimate.document_page_count == 3
    assert (
        build_page_memory_page_limit_rejection(
            job_metadata=_PAGE_MEMORY_METADATA,
            workload_estimate=estimate,
        )
        is None
    )


def test_deck_over_slide_limit_reports_slides_only() -> None:
    estimate = WorkloadEstimate(
        page_count=4 + 30,
        method="pptx_slides",
        embedded_media_durations_ms=(60 * 60 * 1000,),
        embedded_media_page_count=30,
    )

    rejection = build_page_memory_page_limit_rejection(
        job_metadata=_PAGE_MEMORY_METADATA,
        workload_estimate=estimate,
    )

    assert rejection is not None
    assert rejection.details == {
        "violations": [
            {"field": "page_count", "description": "Document has 4 pages, limit is 3"}
        ]
    }


def test_other_tracks_are_not_page_limited() -> None:
    estimate = WorkloadEstimate(page_count=99, method="pdf_metadata")

    assert (
        build_page_memory_page_limit_rejection(
            job_metadata={"parse_track": "chunk"},
            workload_estimate=estimate,
        )
        is None
    )
