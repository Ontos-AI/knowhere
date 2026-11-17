"""Find a slide deck's section structure from its page images.

1. Screen: every slide goes into numbered grids; one VLM call per grid answers
   yes/no per slide: is it structural (agenda / table of contents / divider)?
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

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.services.document_agent.slide_structure.config import (
    GridLayout,
    SlideStructureConfig,
)
from app.services.document_agent.slide_structure.prompts import (
    GAP_PROMPT,
    SCREEN_PROMPT,
    STRUCTURE_PROMPT,
)

VlmCall = Callable[[str, list[str], str], dict[str, Any]]
"""``(prompt, base64 PNG images, usage task) -> parsed JSON object``."""


class SlideGrids(Protocol):
    def grids(
        self, slides: list[int], layout: GridLayout, *, name: str
    ) -> list[str]: ...


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
    grids: SlideGrids,
    config: SlideStructureConfig,
    vlm: VlmCall,
) -> SlideStructure:
    slides = list(range(1, page_count + 1))
    candidates = _screen(slides, grids=grids, config=config, vlm=vlm)
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
        ignored_slides=_slide_numbers(answer.get("ignored_slides"), set(candidates)),
        gap_groups=gap_groups,
    )


def _screen(
    slides: list[int], *, grids: SlideGrids, config: SlideStructureConfig, vlm: VlmCall
) -> list[int]:
    chunks = [
        slides[start : start + config.sheet.cells]
        for start in range(0, len(slides), config.sheet.cells)
    ]
    images = grids.grids(slides, config.sheet, name="screen")
    answers = _parallel(
        [(SCREEN_PROMPT, [image], "screen") for image in images],
        vlm=vlm,
        concurrency=config.vlm_concurrency,
    )
    found: set[int] = set()
    for chunk, answer in zip(chunks, answers):
        found.update(_slide_numbers(answer.get("structural_slides"), set(chunk)))
    return sorted(found)


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
        after = _as_int(gap.get("after_slide"))
        before = _as_int(gap.get("before_slide"))
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
            level = _as_int(expected.get("level"))
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
    grids: SlideGrids,
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
    answers = _parallel(requests, vlm=vlm, concurrency=config.vlm_concurrency)
    for group, answer in zip(groups, answers):
        level_by_title: dict[str, int] = group["expected"]
        span = set(range(group["first"], group["last"] + 1))
        located: dict[str, SlideSection] = {}
        found = answer.get("found")
        for item in found if isinstance(found, list) else []:
            if not isinstance(item, dict) or item.get("expected") not in level_by_title:
                continue
            start = _as_int(item.get("start_slide"))
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


def _parallel(
    requests: list[tuple[str, list[str], str]], *, vlm: VlmCall, concurrency: int
) -> list[dict[str, Any]]:
    if not requests:
        return []
    from gevent.pool import Pool as GeventPool

    pool = GeventPool(size=min(concurrency, len(requests)))
    jobs = [pool.spawn(vlm, prompt, images, task) for prompt, images, task in requests]
    pool.join(raise_error=True)
    return [job.get() for job in jobs]


def _section(item: Any, allowed: set[int]) -> SlideSection | None:
    if not isinstance(item, dict):
        return None
    title = _title(item.get("title"))
    start = _as_int(item.get("start_slide"))
    level = _as_int(item.get("level"))
    if not title or start not in allowed or level is None or level < 1:
        return None
    return SlideSection(title=title, level=level, start_slide=start)


def _slide_numbers(raw: Any, allowed: set[int]) -> list[int]:
    numbers = {_as_int(item) for item in (raw if isinstance(raw, list) else [])}
    return sorted(number for number in numbers if number in allowed)


def _title(raw: Any) -> str:
    return " ".join(str(raw or "").split())


def _as_int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
