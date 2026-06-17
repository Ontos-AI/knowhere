"""Page-to-section mapper: assigns each page a section_path and role.

Given ``SectionSkeleton`` (from C4) and ``PageTagResult`` (from C3),
maps every page to one or more section paths with roles:

- ``primary``   — page is the *start page* of this section
- ``spans``     — page falls within the section range but is not the start
- ``inherited`` — page is not in any section range; inherits the nearest
                  preceding primary section's path

First page with no section → ``<filename>/Root``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from loguru import logger

from app.services.page_memory.page_tagger import PageTagResult
from app.services.page_memory.skeleton_extractor import SectionSkeleton


class SectionRole(str, Enum):
    PRIMARY = "primary"
    SPANS = "spans"
    INHERITED = "inherited"


@dataclass(frozen=True)
class PageSectionMapping:
    """Section assignment for a single page."""

    page_index: int
    section_path: str
    """Primary section_path written to the DataFrame ``path`` column."""

    section_roles: list[dict[str, str]] = field(default_factory=list)
    """All roles: ``[{"section_path": "...", "role": "primary|spans|inherited"}, ...]``."""


def map_pages_to_sections(
    *,
    page_count: int,
    skeletons: list[SectionSkeleton],
    tag_results: list[PageTagResult] | None = None,
    filename: str = "",
) -> list[PageSectionMapping]:
    """Map every page (1..page_count) to a section_path.

    Parameters
    ----------
    page_count:
        Total number of pages.
    skeletons:
        Leaf skeletons from ``skeleton_extractor``.  Each has
        ``section_path``, ``start_page``, ``end_page``.
    tag_results:
        Optional tagger output; ``observed_titles`` can refine primary
        detection (future use — currently unused).
    filename:
        Source filename for the fallback root path.

    Returns
    -------
    list[PageSectionMapping]
        One entry per page, ordered by page_index.
    """
    root_path = f"{filename}/Root" if filename else "Root"

    # Build a sorted list of section ranges
    sorted_skeletons = sorted(skeletons, key=lambda s: (s.start_page, s.level))

    # Pre-compute: for each page, which skeletons cover it
    page_roles: dict[int, list[dict[str, str]]] = {
        page: [] for page in range(1, page_count + 1)
    }

    for skel in sorted_skeletons:
        for page in range(skel.start_page, skel.end_page + 1):
            if page < 1 or page > page_count:
                continue
            role = (
                SectionRole.PRIMARY
                if page == skel.start_page
                else SectionRole.SPANS
            )
            page_roles[page].append(
                {"section_path": skel.section_path, "role": role.value}
            )

    # Assign primary section_path per page
    results: list[PageSectionMapping] = []
    last_primary_path = root_path

    for page in range(1, page_count + 1):
        roles = page_roles.get(page, [])

        if roles:
            # Pick the deepest-level primary as the main path,
            # or fall back to the first spans entry
            primaries = [r for r in roles if r["role"] == SectionRole.PRIMARY.value]
            if primaries:
                main_path = primaries[-1]["section_path"]  # deepest
                last_primary_path = main_path
            else:
                main_path = roles[0]["section_path"]
        else:
            # No skeleton covers this page → inherited
            main_path = last_primary_path
            roles = [
                {"section_path": last_primary_path, "role": SectionRole.INHERITED.value}
            ]

        results.append(
            PageSectionMapping(
                page_index=page,
                section_path=main_path,
                section_roles=roles,
            )
        )

    assigned = sum(1 for r in results if any(
        sr["role"] != SectionRole.INHERITED.value for sr in r.section_roles
    ))
    logger.info(
        "[page_section_mapper] mapped {} pages: {} assigned, {} inherited",
        len(results),
        assigned,
        len(results) - assigned,
    )
    return results
