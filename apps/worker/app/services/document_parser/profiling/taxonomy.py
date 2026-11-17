from __future__ import annotations

from enum import Enum


class PdfRoutingCategory(str, Enum):
    """Page format of a PDF. Whether pages are scanned is ``is_scanned``, not a category."""

    ATLAS = "atlas"
    GENERIC = "generic"
    SLIDES = "slides"

    @classmethod
    def normalize(cls, value: object) -> "PdfRoutingCategory":
        if isinstance(value, cls):
            return value
        raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if raw in {"atlas", "engineering_atlas", "drawing_atlas", "drawing_collection"}:
            return cls.ATLAS
        if raw in {"slide", "slides", "ppt", "pptx", "presentation"}:
            return cls.SLIDES
        return cls.GENERIC


_PERSISTED_SCANNED_ROUTING = "scanned"


def split_persisted_scanned_routing(
    routing_category: object, is_scanned: bool
) -> tuple[PdfRoutingCategory, bool]:
    """Read a persisted profile whose routing may still be the old ``scanned`` value.

    Profiles written before ``scanned`` left the routing categories carry it in
    ``routing_category``; it now means ``is_scanned``.
    """
    persisted_scanned = routing_category == _PERSISTED_SCANNED_ROUTING
    return PdfRoutingCategory.normalize(routing_category), is_scanned or persisted_scanned


__all__ = ["PdfRoutingCategory", "split_persisted_scanned_routing"]
