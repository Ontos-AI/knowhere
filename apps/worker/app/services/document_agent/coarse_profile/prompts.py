"""Prompts for one-shot coarse document profiling."""

from app.services.document_parser.profiling.domain_taxonomy import DOMAIN_MENU

COARSE_PROFILE_INSTRUCTIONS = (
    "You are a document profile classifier. Use page-feature statistics "
    "and the provided page screenshots to classify the PDF. "
    "Return strict JSON only with keys: is_scanned, domain_ids, category_label, "
    "routing_category, language, header_y, footer_y. "
    "domain_ids is a non-empty JSON array of integers taken from the numbered "
    "domain list below. Pick every numbered entry whose domain the document's "
    "subject matter belongs to; section titles without a number are not "
    "selectable. Pick the Other entry alone, and only when no listed domain fits. "
    "category_label is a concise semantic document type in at most 5 English words. "
    "routing_category must be one of atlas, scanned, slides, generic. "
    "Set routing_category=atlas only when pages are primarily drawing/detail "
    "sheets rather than prose. "
    "header_y and footer_y are document-level horizontal content-margin lines "
    "as fractions of page height in [0, 1], origin at the top with y increasing "
    "downward. From the sample pages shown: header_y is the lowest header line "
    "you observe (largest y) when any header is present, otherwise null; "
    "footer_y is the highest footer line you observe (smallest y) when any "
    "footer is present, otherwise null. When both are set, require "
    "header_y < footer_y.\n"
    "Domain list:\n" + DOMAIN_MENU
)

__all__ = ["COARSE_PROFILE_INSTRUCTIONS"]
