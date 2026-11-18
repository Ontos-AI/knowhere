from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.services.document_parser.profiling.domain_taxonomy import OTHER_CODE
from app.services.document_parser.profiling.taxonomy import PdfRoutingCategory


@dataclass
class TocEvidence:
    page_index: int
    source: str
    reason: str = ""


@dataclass
class ParserTocProfile:
    toc_pages: list[int] = field(default_factory=list)
    hierarchies: list[dict[str, Any]] | None = None
    evidence: list[TocEvidence] = field(default_factory=list)
    source: str = "none"
    method: str = "none"
    notes: str = ""


@dataclass
class ParserDocumentProfile:
    """Page-memory PDF profile: coarse classification, TOC and anatomy."""

    file_type: str
    category: list[str] = field(default_factory=lambda: [OTHER_CODE])
    category_label: str = "unknown document"
    routing_category: PdfRoutingCategory = PdfRoutingCategory.GENERIC
    is_scanned: bool = False
    page_count: int = 0
    language: str = "unknown"
    toc: ParserTocProfile = field(default_factory=ParserTocProfile)
    anatomy: Any | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    page_full_text_cache: dict[int, str] = field(default_factory=dict)
    page_body_text_cache: dict[int, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("page_full_text_cache", None)
        data.pop("page_body_text_cache", None)
        data["routing_category"] = self.routing_category.value
        if self.anatomy is not None and hasattr(self.anatomy, "to_dict"):
            data["anatomy"] = self.anatomy.to_dict()
        else:
            data["anatomy"] = None
        return data


__all__ = ["ParserDocumentProfile", "ParserTocProfile", "TocEvidence"]
