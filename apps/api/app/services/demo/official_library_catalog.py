"""Official Library metadata for API-owned demo sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


OfficialLibraryStatus = Literal["ready", "planned"]


@dataclass(frozen=True)
class OfficialLibraryCategoryDefinition:
    category_id: str
    label: str
    description: str


@dataclass(frozen=True)
class OfficialLibrarySourceDefinition:
    library_source_id: str
    category_id: str
    title: str
    source_url: str
    mime_type: str
    status: OfficialLibraryStatus = "planned"
    demo_source_id: str | None = None


_OFFICIAL_LIBRARY_CATEGORIES: tuple[OfficialLibraryCategoryDefinition, ...] = (
    OfficialLibraryCategoryDefinition(
        category_id="financial-reports",
        label="Financial reports",
        description="Company filings, earnings materials, and investor reports.",
    ),
    OfficialLibraryCategoryDefinition(
        category_id="stem-books",
        label="STEM books",
        description="Open lecture notes, books, and course materials.",
    ),
)

_OFFICIAL_LIBRARY_SOURCES: tuple[OfficialLibrarySourceDefinition, ...] = (
    OfficialLibrarySourceDefinition(
        library_source_id="financial-spacex-s1",
        category_id="financial-reports",
        title="spacex-s1.pdf",
        source_url="https://data.olivierroy.dev/spacex-s1.pdf",
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-spacex-s1",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="financial-micron-report-530bd7ed",
        category_id="financial-reports",
        title="Micron investor report 530bd7ed.pdf",
        source_url=(
            "https://investors.micron.com/static-files/"
            "530bd7ed-a8c8-4687-af4a-8c129f740e09"
        ),
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-financial-micron-report-530bd7ed",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="financial-micron-report-9c0becf5",
        category_id="financial-reports",
        title="Micron investor report 9c0becf5.pdf",
        source_url=(
            "https://investors.micron.com/static-files/"
            "9c0becf5-df56-4eec-bd67-453dda68b273"
        ),
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-financial-micron-report-9c0becf5",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="stem-transformers-tutorial",
        category_id="stem-books",
        title="Transformers Tutorial.pdf",
        source_url=(
            "https://aihub.csic.es/wp-content/uploads/2024/07/"
            "Transformers-tutorial.pdf"
        ),
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-stem-transformers-tutorial",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="stem-information-theory",
        category_id="stem-books",
        title="Information Theory.pdf",
        source_url="https://people.lids.mit.edu/yp/homepage/data/itbook-export.pdf",
        mime_type="application/pdf",
    ),
)


def get_official_library_catalog(
    source_payload_by_demo_source_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return Official Library metadata with ready demo source links."""
    return {
        "categories": [
            {
                "category_id": category.category_id,
                "label": category.label,
                "description": category.description,
            }
            for category in _OFFICIAL_LIBRARY_CATEGORIES
        ],
        "sources": [
            _source_payload(
                source=source,
                source_payload_by_demo_source_id=source_payload_by_demo_source_id,
            )
            for source in _OFFICIAL_LIBRARY_SOURCES
        ],
    }


def get_official_library_source_payload_by_demo_source_id() -> dict[str, dict[str, Any]]:
    """Return ready Official Library metadata keyed by demo source id."""
    return {
        source.demo_source_id: _source_payload(
            source=source,
            source_payload_by_demo_source_id={},
        )
        for source in _OFFICIAL_LIBRARY_SOURCES
        if source.demo_source_id is not None and source.status == "ready"
    }


def iter_official_library_sources() -> tuple[OfficialLibrarySourceDefinition, ...]:
    return _OFFICIAL_LIBRARY_SOURCES


def iter_official_library_categories() -> tuple[OfficialLibraryCategoryDefinition, ...]:
    return _OFFICIAL_LIBRARY_CATEGORIES


def _source_payload(
    *,
    source: OfficialLibrarySourceDefinition,
    source_payload_by_demo_source_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    ready_source = (
        source_payload_by_demo_source_id.get(source.demo_source_id)
        if source.demo_source_id
        else None
    )
    payload: dict[str, Any] = {
        "library_source_id": source.library_source_id,
        "category_id": source.category_id,
        "title": source.title,
        "source_url": source.source_url,
        "mime_type": source.mime_type,
        "status": "ready" if ready_source else source.status,
    }
    if source.demo_source_id is not None:
        payload["demo_source_id"] = source.demo_source_id
    if ready_source is not None:
        payload["canonical_document_id"] = ready_source["canonical_document_id"]
        payload["size_bytes"] = ready_source["size_bytes"]
        payload["chunk_count"] = ready_source["chunk_count"]
        payload["original_file"] = ready_source["original_file"]
    return payload
