from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory


@dataclass
class TocEvidence:
    page_index: int
    source: str
    confidence: float
    reason: str = ""


@dataclass
class ParserTocProfile:
    toc_pages: list[int] = field(default_factory=list)
    hierarchies: list[dict[str, Any]] | None = None
    evidence: list[TocEvidence] = field(default_factory=list)
    source: str = "none"
    method: str = "none"
    notes: str = ""

    @property
    def has_toc(self) -> bool:
        return bool(self.toc_pages or self.hierarchies)


@dataclass
class ParserDocumentProfile:
    """Parser-entry document profile used for routing and PDF anatomy reuse."""

    file_type: str
    category: str = "unknown document"
    routing_category: PdfRoutingCategory = PdfRoutingCategory.GENERIC
    is_scanned: bool = False
    page_count: int = 0
    language: str = "unknown"
    reasoning: str = ""
    category_rationale: str = ""
    toc: ParserTocProfile = field(default_factory=ParserTocProfile)
    granularity: str = "page"
    anatomy: Any | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def is_pdf(self) -> bool:
        return self.file_type == "pdf"

    @property
    def is_atlas(self) -> bool:
        return self.routing_category is PdfRoutingCategory.ATLAS

    @property
    def has_structural_anatomy(self) -> bool:
        return self.anatomy is not None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["routing_category"] = self.routing_category.value
        if self.anatomy is not None and hasattr(self.anatomy, "to_dict"):
            data["anatomy"] = self.anatomy.to_dict()
        else:
            data["anatomy"] = None
        return data

    def summary(self) -> str:
        parts = (
            f"[{self.file_type.upper()}] category={self.category}, "
            f"routing={self.routing_category.value}, "
            f"scanned={self.is_scanned}, pages={self.page_count}"
        )
        if self.toc.has_toc:
            parts += f", toc={self.toc.method}"
        if self.has_structural_anatomy:
            parts += ", anatomy=True"
        return parts


__all__ = ["ParserDocumentProfile", "ParserTocProfile", "TocEvidence"]
