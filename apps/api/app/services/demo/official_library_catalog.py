"""Official Library categories for the shared demo corpus.

Only the categories are code. The sources listed under them are the active
shared demo documents whose stored `category_id` names one of these
categories, so adding, recategorizing or archiving a demo document through the
API changes the Official Library without a release.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OfficialLibraryCategoryDefinition:
    category_id: str
    label: str
    description: str


# A demo document outside the Official Library: searchable in the shared
# corpus, but not listed under any category.
UNCATEGORIZED_DEMO_CATEGORY_ID: str = "other"

_OFFICIAL_LIBRARY_CATEGORIES: tuple[OfficialLibraryCategoryDefinition, ...] = (
    OfficialLibraryCategoryDefinition(
        category_id="financial-reports",
        label="Financial reports",
        description="Company filings, earnings materials, and investor reports.",
    ),
    OfficialLibraryCategoryDefinition(
        category_id="research-papers",
        label="Research papers",
        description="Curated academic papers and surveys.",
    ),
    OfficialLibraryCategoryDefinition(
        category_id="stem-books",
        label="STEM books",
        description="Open lecture notes, books, and course materials.",
    ),
)
_OFFICIAL_LIBRARY_CATEGORY_IDS: frozenset[str] = frozenset(
    category.category_id for category in _OFFICIAL_LIBRARY_CATEGORIES
)


def iter_official_library_categories() -> tuple[OfficialLibraryCategoryDefinition, ...]:
    return _OFFICIAL_LIBRARY_CATEGORIES


def is_assignable_demo_category(category_id: str) -> bool:
    """Whether a maintainer may file a demo document under this category id."""
    return (
        category_id in _OFFICIAL_LIBRARY_CATEGORY_IDS
        or category_id == UNCATEGORIZED_DEMO_CATEGORY_ID
    )


def get_official_library_catalog(
    sources: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the categories and the given sources that belong to one of them."""
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
            dict(source)
            for source in sources
            if source.get("category_id") in _OFFICIAL_LIBRARY_CATEGORY_IDS
        ],
    }
