"""Page processing plan: maps page labels to tagging strategy.

Reads ``PageLabel.kind`` + ``PageFeature`` and assigns each page
one of three strategies:

- ``vlm_lite``  — send the page image to VLM for summary/entities
- ``text_only`` — use raw text via page-memory-text-tag (no VLM call)
- ``skip_tagging`` — blank-like page, preserve image only

Set ``tag_mode=text`` to force all non-skip pages to ``text_only``.
Default is ``vlm``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.services.document_agent.manifest import PageFeature, PageLabel


class PageProcessingStrategy(str, Enum):
    """Tagging strategy for a single page."""

    VLM_LITE = "vlm_lite"
    TEXT_ONLY = "text_only"
    SKIP_TAGGING = "skip_tagging"


@dataclass(frozen=True)
class PagePlan:
    """Processing plan for a single page."""

    page_index: int
    strategy: PageProcessingStrategy
    reason: str


def derive_page_processing_plan(
    *,
    page_count: int,
    page_labels: list[PageLabel],
    page_features: list[PageFeature],
    tag_mode: str = "vlm",
) -> list[PagePlan]:
    """Assign a processing strategy to every page.

    Rules:
    - ``low_content`` + ``is_blank_like`` → ``skip_tagging``
    - ``tag_mode=text`` → ``text_only`` for all remaining pages
    - ``table_heavy`` with sufficient raw text → ``text_only``
    - everything else (normal / image_heavy / landscape) → ``vlm_lite``

    Returns one ``PagePlan`` per page (1-indexed), ordered by page_index.
    """
    label_map: dict[int, PageLabel] = {label.page: label for label in page_labels}
    feature_map: dict[int, PageFeature] = {feat.page: feat for feat in page_features}

    plans: list[PagePlan] = []
    for page in range(1, page_count + 1):
        label = label_map.get(page)
        feature = feature_map.get(page)
        strategy, reason = _classify_page(label, feature, tag_mode=tag_mode)
        plans.append(PagePlan(page_index=page, strategy=strategy, reason=reason))

    return plans


# ── minimum raw text length for table_heavy → text_only ──────────────
_TABLE_TEXT_THRESHOLD = 200

def _classify_page(
    label: PageLabel | None,
    feature: PageFeature | None,
    *,
    tag_mode: str,
) -> tuple[PageProcessingStrategy, str]:
    """Determine the strategy for a single page."""
    kind = label.kind if label else "normal"
    is_blank = feature.is_blank_like if feature else False
    text_len = feature.raw_text_length if feature else 0

    if kind == "low_content" and is_blank:
        return PageProcessingStrategy.SKIP_TAGGING, "low_content + blank_like"

    if tag_mode.strip().lower() == "text":
        return PageProcessingStrategy.TEXT_ONLY, f"text_mode_override kind={kind}"

    if kind == "table_heavy" and text_len >= _TABLE_TEXT_THRESHOLD:
        return PageProcessingStrategy.TEXT_ONLY, f"table_heavy with {text_len} chars"

    return PageProcessingStrategy.VLM_LITE, f"kind={kind}"
