from __future__ import annotations

from enum import Enum


class PdfRoutingCategory(str, Enum):
    ATLAS = "atlas"
    GENERIC = "generic"
    SCANNED = "scanned"
    SLIDES = "slides"

    @classmethod
    def normalize(cls, value: object) -> "PdfRoutingCategory":
        raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if raw in {"atlas", "engineering_atlas", "drawing_atlas", "drawing_collection"}:
            return cls.ATLAS
        if raw in {"scan", "scanned", "scanned_pdf", "image_only"}:
            return cls.SCANNED
        if raw in {"slide", "slides", "ppt", "pptx", "presentation"}:
            return cls.SLIDES
        return cls.GENERIC


__all__ = ["PdfRoutingCategory"]
