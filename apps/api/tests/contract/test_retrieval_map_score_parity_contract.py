"""Contract tests for map score pooling semantics."""

from __future__ import annotations

from typing import Final

import pytest

from shared.services.retrieval.nav.nav_map_scores import _pool_unit_scores_to_tree


_CASES: Final[tuple[tuple[dict[str, list[str]], set[str], dict[str, float]], ...]] = (
    (
        {"root-a": ["section-a", "section-b"], "section-a": [], "section-b": []},
        {"section-a", "section-b"},
        {"section-a": 0.4, "section-b": 0.8, "root-a__self": 0.2},
    ),
    (
        {
            "root-a": ["parent-a"],
            "parent-a": ["leaf-a", "leaf-b"],
            "leaf-a": [],
            "leaf-b": [],
            "root-b": ["leaf-c"],
            "leaf-c": [],
        },
        {"leaf-a", "leaf-b", "leaf-c"},
        {
            "leaf-a": 0.9,
            "leaf-b": 0.3,
            "leaf-c": 0.7,
            "parent-a__self": 0.95,
        },
    ),
)


def _legacy_pool(
    children_map: dict[str, list[str]],
    leaves: set[str],
    unit_scores: dict[str, float],
) -> dict[str, float]:
    map_scores = {
        leaf_id: float(unit_scores.get(leaf_id, 0.0) or 0.0) for leaf_id in leaves
    }

    def score_node(section_id: str) -> float:
        if section_id in map_scores:
            return map_scores[section_id]
        children = children_map.get(section_id) or []
        if not children:
            score = float(unit_scores.get(section_id, 0.0) or 0.0)
            map_scores[section_id] = score
            return score
        descendants: list[str] = []

        def collect(section: str) -> None:
            nested = children_map.get(section) or []
            if not nested:
                if section in leaves:
                    descendants.append(section)
                return
            for child in nested:
                collect(child)

        collect(section_id)
        parts = [float(unit_scores.get(leaf_id, 0.0) or 0.0) for leaf_id in descendants]
        self_key = f"{section_id}__self"
        if self_key in unit_scores:
            parts.append(float(unit_scores[self_key]))
        score = float(max(parts)) if parts else 0.0
        map_scores[section_id] = score
        return score

    for section_id in children_map:
        score_node(section_id)
    return map_scores


@pytest.mark.parametrize("children_map, leaves, unit_scores", _CASES)
def test_map_score_pooling_preserves_legacy_semantics(
    children_map: dict[str, list[str]],
    leaves: set[str],
    unit_scores: dict[str, float],
) -> None:
    assert _pool_unit_scores_to_tree(children_map, leaves, unit_scores) == _legacy_pool(
        children_map, leaves, unit_scores
    )
