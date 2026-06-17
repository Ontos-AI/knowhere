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
        category_id="research-papers",
        label="Research papers",
        description="Curated academic papers and technical reports.",
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
        library_source_id="financial-nvda-q1-fy27-earnings-call",
        category_id="financial-reports",
        title="NVDA Q1 FY27 Earnings Call Transcript.pdf",
        source_url=(
            "https://s201.q4cdn.com/141608511/files/doc_financials/2027/q1/"
            "NVDA-Q1-2027-Earnings-Call-20-May-2026-5_00-PM-ET.pdf"
        ),
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-financial-nvda-q1-fy27-earnings-call",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="financial-nvda-q4-fy26-earnings-call",
        category_id="financial-reports",
        title="NVDA Q4 FY26 Earnings Call Transcript.pdf",
        source_url=(
            "https://s201.q4cdn.com/141608511/files/doc_financials/2026/q4/"
            "NVDA-Q4-2026-Earnings-Call-25-February-2026-5_00-PM-ET.pdf"
        ),
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-financial-nvda-q4-fy26-earnings-call",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="financial-nvda-q1-fy27-cfo-commentary",
        category_id="financial-reports",
        title="NVIDIA Q1 FY27 CFO Commentary.pdf",
        source_url=(
            "https://s201.q4cdn.com/141608511/files/doc_financials/2027/Q127/"
            "Q1FY27-CFO-Commentary.pdf"
        ),
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-financial-nvda-q1-fy27-cfo-commentary",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="financial-nvda-q1-fy27-results",
        category_id="financial-reports",
        title="NVIDIA Q1 FY27 Results.pdf",
        source_url=(
            "https://s201.q4cdn.com/141608511/files/doc_financials/2027/q1/"
            "927dc2d6-a76c-4006-9f34-8769b2c665fb.pdf"
        ),
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-financial-nvda-q1-fy27-results",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="financial-nvda-q1-fy27-presentation",
        category_id="financial-reports",
        title="NVIDIA Q1 FY27 Quarterly Presentation.pdf",
        source_url=(
            "https://s201.q4cdn.com/141608511/files/doc_financials/2027/Q127/"
            "NVDA-F1Q27-Quarterly-Presentation-FINAL.pdf"
        ),
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-financial-nvda-q1-fy27-presentation",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="financial-nvda-fy26-annual-report",
        category_id="financial-reports",
        title="NVIDIA FY26 Annual Report.pdf",
        source_url=(
            "https://s201.q4cdn.com/141608511/files/doc_financials/2026/ar/"
            "2026-Annual-Report-Web.pdf"
        ),
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-financial-nvda-fy26-annual-report",
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
        library_source_id="financial-goog-10-k-2025",
        category_id="financial-reports",
        title="GOOG 10-K 2025.pdf",
        source_url=(
            "https://s206.q4cdn.com/479360582/files/doc_financials/2025/q4/"
            "GOOG-10-K-2025.pdf"
        ),
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-financial-goog-10-k-2025",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="financial-meta-q1-2026-results",
        category_id="financial-reports",
        title="Meta Q1 2026 Exhibit 99.1.pdf",
        source_url=(
            "https://s21.q4cdn.com/399680738/files/doc_financials/2026/q1/"
            "Meta-03-31-2026-Exhibit-99-1_final.pdf"
        ),
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-financial-meta-q1-2026-results",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="financial-meta-q1-2026-presentation",
        category_id="financial-reports",
        title="Meta Q1 2026 Earnings Presentation.pdf",
        source_url=(
            "https://s21.q4cdn.com/399680738/files/doc_financials/2026/q1/"
            "Earnings-Presentation-Q1-2026.pdf"
        ),
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-financial-meta-q1-2026-presentation",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="financial-meta-q1-2026-earnings-call",
        category_id="financial-reports",
        title="Meta Q1 2026 Earnings Call Transcript.pdf",
        source_url=(
            "https://s21.q4cdn.com/399680738/files/doc_financials/2026/q1/"
            "META-Q1-2026-Earnings-Call-Transcript.pdf"
        ),
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-financial-meta-q1-2026-earnings-call",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="financial-microsoft-2025-annual-report",
        category_id="financial-reports",
        title="Microsoft 2025 Annual Report.docx",
        source_url=(
            "https://cdn-dynmedia-1.microsoft.com/is/content/microsoftcorp/"
            "2025_AnnualReport.docx"
        ),
        mime_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        status="ready",
        demo_source_id="demo-financial-microsoft-2025-annual-report",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="financial-alibaba-fy2026-interim-report",
        category_id="financial-reports",
        title="Alibaba Fiscal Year 2026 Interim Report.pdf",
        source_url=(
            "https://data.alibabagroup.com/ecms-files/1514443390/"
            "2b483071-b7a2-45ea-b6cd-f4d35861bab0/"
            "Fiscal%20Year%202026%20Interim%20Report.pdf"
        ),
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-financial-alibaba-fy2026-interim-report",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="stem-jurafsky-transformers",
        category_id="stem-books",
        title="Speech and Language Processing, Chapter 8: Transformers.pdf",
        source_url="https://web.stanford.edu/~jurafsky/slp3/8.pdf",
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-stem-jurafsky-transformers",
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
        library_source_id="stem-deep-learning-transformer-network",
        category_id="stem-books",
        title="Deep Learning - Transformer Network.pdf",
        source_url="https://homel.vsb.cz/~pla06/files/dl/dl_10.pdf",
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-stem-deep-learning-transformer-network",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="stem-information-theory",
        category_id="stem-books",
        title="Information Theory.pdf",
        source_url="https://people.lids.mit.edu/yp/homepage/data/itbook-export.pdf",
        mime_type="application/pdf",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="stem-probability-theory-lecture-notes",
        category_id="stem-books",
        title="Probability Theory Lecture Notes.pdf",
        source_url="https://www.math.union.edu/~marianop/ma41600sum19/Notes.pdf",
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-stem-probability-theory-lecture-notes",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="stem-statistical-learning",
        category_id="stem-books",
        title="Statistical Learning Notes.pdf",
        source_url="http://www.statslab.cam.ac.uk/~rds37/teaching/stat_learning/notes.pdf",
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-stem-statistical-learning",
    ),
    OfficialLibrarySourceDefinition(
        library_source_id="stem-introduction-statistical-learning-theory",
        category_id="stem-books",
        title="Introduction to Statistical Learning Theory.pdf",
        source_url="https://cciliber.github.io/intro-slt/slides/lec1.pdf",
        mime_type="application/pdf",
        status="ready",
        demo_source_id="demo-stem-introduction-statistical-learning-theory",
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
            if _is_publishable_ready_source(
                source=source,
                source_payload_by_demo_source_id=source_payload_by_demo_source_id,
            )
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


def _is_publishable_ready_source(
    *,
    source: OfficialLibrarySourceDefinition,
    source_payload_by_demo_source_id: dict[str, dict[str, Any]],
) -> bool:
    if source.status != "ready" or source.demo_source_id is None:
        return False
    return source.demo_source_id in source_payload_by_demo_source_id


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
