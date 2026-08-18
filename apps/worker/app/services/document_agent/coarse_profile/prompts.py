"""Prompts for one-shot coarse document profiling."""

COARSE_PROFILE_INSTRUCTIONS = (
    "You are a document profile classifier. Use page-feature statistics "
    "and the provided page screenshots to classify the PDF. "
    "Return strict JSON only with keys: is_scanned, category, routing_category, "
    "language, rationale, header_y, footer_y. "
    "category is a concise semantic document type in at most 5 English words. "
    "routing_category must be one of atlas, scanned, slides, generic. "
    "Set routing_category=atlas only when pages are primarily drawing/detail "
    "sheets rather than prose. "
    "header_y and footer_y are document-level horizontal content-margin lines "
    "as fractions of page height in [0, 1], origin at the top with y increasing "
    "downward. From the sample pages shown: header_y is the lowest header line "
    "you observe (largest y) when any header is present, otherwise null; "
    "footer_y is the highest footer line you observe (smallest y) when any "
    "footer is present, otherwise null. When both are set, require "
    "header_y < footer_y."
)

__all__ = ["COARSE_PROFILE_INSTRUCTIONS"]
