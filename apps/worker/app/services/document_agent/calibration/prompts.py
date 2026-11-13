"""Shared VLM prompts for printed→physical section-start verification."""

from __future__ import annotations

from typing import Any

# Multi-page batch verify (null-page / Phase-2 helpers).
SECTION_START_ANSWER_KEYS = {
    "found": (
        "boolean, true when that section starts on one of these pages "
        "(exact heading or accepted variant)"
    ),
    "found_page": "number|null, the physical page number where it starts",
}

# Single-page concurrent scan (calibration Phase-1 forward scan).
SECTION_START_PAGE_ANSWER_KEYS = {
    "found": (
        "boolean, true when THIS page's main heading starts the given section "
        "(exact or accepted variant)"
    ),
    "reason": "string, at most 20 Chinese characters explaining the decision",
}


def _variant_rules() -> str:
    return (
        "Count as a match when either:\n"
        "(1) Exact: the page's main heading matches the given title as written; or\n"
        "(2) Variant: the page's main heading is clearly the same section, "
        "even if a number/letter prefix differs or a document-id/code suffix "
        "appears on only one side "
        "(e.g. given title '3.2 Foo' ↔ page heading 'Foo'; "
        "given title 'Foo (DOC-12)' ↔ page heading 'Foo'; "
        "given title 'A.1 Foo Bar' ↔ page heading 'Foo Bar').\n"
        "Do NOT count: a contents-list line, a running header/footer, or "
        "a passing mention inside ordinary body paragraphs."
    )


def build_section_start_question(title: str) -> str:
    """Ask whether ``title`` starts as a body heading on the provided pages."""
    return (
        f"Does the section titled {title!r} START on one of these pages as a "
        "body heading (where that section begins)?\n"
        f"{_variant_rules()}\n"
        "Report the physical page number from the page label above each image."
    )


def build_section_start_page_question(title: str) -> str:
    """Ask whether ``title`` starts as a body heading on this single page."""
    return (
        f"Does the section titled {title!r} START on THIS page as a "
        "body heading (where that section begins)?\n"
        f"{_variant_rules()}\n"
        "Return found true or false, and a reason of at most 20 Chinese characters."
    )


def coerce_found(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def coerce_found_page(value: Any, *, pages: list[int]) -> int | None:
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page in pages else None


def coerce_reason(value: Any) -> str:
    return str(value or "").strip().replace("\n", " ")
