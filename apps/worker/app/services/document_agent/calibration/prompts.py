"""Shared VLM prompts for printed→physical section-start verification."""

from __future__ import annotations

from typing import Any

SECTION_START_ANSWER_KEYS = {
    "found": "boolean, true only when the section heading starts on one of these pages",
    "found_page": "number|null, the physical page number where it starts",
}


def build_section_start_question(title: str) -> str:
    """Ask whether ``title`` starts as a body heading on the provided pages."""
    return (
        f"Does the section titled {title!r} START on one of these pages, as a "
        "body heading? A table-of-contents line, a running header or footer, or "
        "a passing mention in body text does not count. Report the physical page "
        "number printed in the page label above each image."
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
