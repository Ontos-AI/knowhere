"""Page processing plan: maps page labels to tagging strategy.

Reads ``PageLabel.kind`` + ``PageFeature`` and assigns each page
one of two strategies:

- ``vlm_lite``  — send the page image to VLM for summary/entities
- ``skip_tagging`` — blank-like page, preserve image only
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.services.document_agent.manifest import PageFeature, PageLabel


class PageProcessingStrategy(str, Enum):
    """Tagging strategy for a single page."""

    VLM_LITE = "vlm_lite"
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
) -> list[PagePlan]:
    """Assign a processing strategy to every page.

    Rules:
    - ``is_blank_like`` → ``skip_tagging``
    - everything else → ``vlm_lite``

    Returns one ``PagePlan`` per page (1-indexed), ordered by page_index.
    """
    label_map: dict[int, PageLabel] = {label.page: label for label in page_labels}
    feature_map: dict[int, PageFeature] = {feat.page: feat for feat in page_features}

    plans: list[PagePlan] = []
    for page in range(1, page_count + 1):
        label = label_map.get(page)
        feature = feature_map.get(page)
        strategy, reason = _classify_page(label, feature)
        plans.append(PagePlan(page_index=page, strategy=strategy, reason=reason))

    return plans


def _classify_page(
    label: PageLabel | None,
    feature: PageFeature | None,
) -> tuple[PageProcessingStrategy, str]:
    """Determine the strategy for a single page."""
    kind = label.kind if label else "normal"
    is_blank = feature.is_blank_like if feature else False

    if is_blank:
        return PageProcessingStrategy.SKIP_TAGGING, "blank_like"

    return PageProcessingStrategy.VLM_LITE, f"kind={kind}"
