"""Find a slide deck's section structure from its page images.

1. Candidates: the structural slides the page screen found (TOC / agenda and
   divider slides), given by the caller.
2. Structure: one VLM call over all candidate slides drops false candidates,
   returns the sections (title, start slide, level) and infers gaps: sections
   the evidence says exist but no candidate locates.
3. Gap fill: one grid look per gap group (all expected sections that must lie
   in the same slide range, whatever their level, so the model weighs them
   against each other). A group is kept only when every expected section in it
   is located. A partial
   fill would leave the unlocated siblings inside a filled section's range,
   where FINE could only nest them under it; unfilled, FINE finds them at the
   right level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.document_agent.page_screen.vision import (
    PageGrids,
    VlmCall,
    as_int,
    page_numbers,
    parallel_vlm,
)
from app.services.document_agent.slide_structure.config import SlideStructureConfig
from app.services.document_agent.slide_structure.prompts import (
    GAP_PROMPT,
    STRUCTURE_PROMPT,
)


@dataclass(frozen=True)
class SlideSection:
    title: str
    level: int
    start_slide: int
    filled: bool = False


@dataclass
class SlideStructure:
    sections: list[SlideSection]
    candidates: list[int]
    ignored_slides: list[int] = field(default_factory=list)
    gap_groups: list[dict[str, Any]] = field(default_factory=list)


def detect_slide_structure(
    *,
    page_count: int,
    candidates: list[int],
    grids: PageGrids,
    config: SlideStructureConfig,
    vlm: VlmCall,
) -> SlideStructure:
    if not candidates:
        return SlideStructure(sections=[], candidates=[])

    images = grids.grids(candidates, config.candidates, name="candidates")
    answer = vlm(STRUCTURE_PROMPT.format(page_count=page_count), images, "structure")
    sections = _located_sections(answer.get("sections"), candidates)
    gap_groups = _gap_groups(answer.get("gaps"), page_count)
    _fill_gaps(gap_groups, grids=grids, config=config, vlm=vlm)
    for group in gap_groups:
        if group["accepted"]:
            sections.extend(group["sections"])
    return SlideStructure(
        sections=sorted(sections, key=lambda item: (item.start_slide, item.level)),
        candidates=candidates,
        ignored_slides=page_numbers(answer.get("ignored_slides"), set(candidates)),
        gap_groups=gap_groups,
    )


def _located_sections(raw: Any, candidates: list[int]) -> list[SlideSection]:
    """Sections whose start is a candidate slide, as the structure prompt requires."""
    allowed = set(candidates)
    sections: list[SlideSection] = []
    for item in raw if isinstance(raw, list) else []:
        section = _section(item, allowed)
        if section is not None:
            sections.append(section)
    return sections


def _gap_groups(raw: Any, page_count: int) -> list[dict[str, Any]]:
    """Group expected sections by the slide range they must lie in.

    Each group maps its expected titles to their levels.
    """
    groups: dict[tuple[int, int], dict[str, Any]] = {}
    for gap in raw if isinstance(raw, list) else []:
        if not isinstance(gap, dict):
            continue
        after = as_int(gap.get("after_slide"))
        before = as_int(gap.get("before_slide"))
        if after is None:
            continue
        first = after + 1
        last = page_count if before is None else before - 1
        if not 1 <= first <= last <= page_count:
            continue
        expected_items = gap.get("expected")
        for expected in expected_items if isinstance(expected_items, list) else []:
            if not isinstance(expected, dict):
                continue
            title = _title(expected.get("title"))
            level = as_int(expected.get("level"))
            if not title or level is None or level < 1:
                continue
            group = groups.setdefault(
                (first, last), {"first": first, "last": last, "expected": {}}
            )
            group["expected"].setdefault(title, level)
    return list(groups.values())


def _fill_gaps(
    groups: list[dict[str, Any]],
    *,
    grids: PageGrids,
    config: SlideStructureConfig,
    vlm: VlmCall,
) -> None:
    requests = []
    for group in groups:
        span = list(range(group["first"], group["last"] + 1))
        name = f"gap_{group['first']}-{group['last']}"
        expected = "\n".join(f"- {title}" for title in group["expected"])
        requests.append(
            (
                GAP_PROMPT.format(expected=expected),
                grids.grids(span, config.sheet, name=name),
                "gap_fill",
            )
        )
    answers = parallel_vlm(requests, vlm=vlm, concurrency=config.vlm_concurrency)
    for group, answer in zip(groups, answers):
        level_by_title: dict[str, int] = group["expected"]
        span = set(range(group["first"], group["last"] + 1))
        located: dict[str, SlideSection] = {}
        found = answer.get("found")
        for item in found if isinstance(found, list) else []:
            if not isinstance(item, dict) or item.get("expected") not in level_by_title:
                continue
            start = as_int(item.get("start_slide"))
            if start is None or start not in span:
                continue
            located[item["expected"]] = SlideSection(
                title=_title(item.get("title")) or item["expected"],
                level=level_by_title[item["expected"]],
                start_slide=start,
                filled=True,
            )
        group["found"] = found if isinstance(found, list) else []
        group["accepted"] = located.keys() == level_by_title.keys()
        group["sections"] = list(located.values()) if group["accepted"] else []


def _section(item: Any, allowed: set[int]) -> SlideSection | None:
    if not isinstance(item, dict):
        return None
    title = _title(item.get("title"))
    start = as_int(item.get("start_slide"))
    level = as_int(item.get("level"))
    if not title or start not in allowed or level is None or level < 1:
        return None
    return SlideSection(title=title, level=level, start_slide=start)


def _title(raw: Any) -> str:
    return " ".join(str(raw or "").split())
