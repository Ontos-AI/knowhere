"""sort_skeletons must not alphabetize same-page VLM/TOC order."""

from __future__ import annotations

from app.services.page_memory._serialization import (
    build_hierarchy_tree,
    serialize_hierarchy_artifact,
)
from app.services.page_memory._utils import sort_skeletons
from app.services.page_memory.skeleton_extractor import SectionSkeleton


def _skel(path: str, title: str, *, start_page: int, level: int = 3) -> SectionSkeleton:
    parent = "/".join(path.split("/")[:-1]) or None
    return SectionSkeleton(
        section_path=path,
        title=title,
        level=level,
        start_page=start_page,
        end_page=start_page,
        parent_path=parent,
        evidence={},
    )


def test_same_page_siblings_keep_input_order_not_alphabetical() -> None:
    base = "doc.pdf/Section D/Part D3 Construction of exits"
    # VLM / reading order: Separation (D3D5) before Open access (D3D6)
    page_197 = [
        _skel(
            f"{base}/Non-fire-isolated stairways and ramps",
            "Non-fire-isolated stairways and ramps",
            start_page=197,
        ),
        _skel(
            f"{base}/Separation of rising and descending stair flights",
            "Separation of rising and descending stair flights",
            start_page=197,
        ),
        _skel(
            f"{base}/Open access ramps and balconies",
            "Open access ramps and balconies",
            start_page=197,
        ),
        _skel(f"{base}/Smoke lobbies", "Smoke lobbies", start_page=197),
    ]
    # Later page first in input — page sort must move it after, without
    # alphabetizing the same-page block.
    mixed = [
        _skel(f"{base}/Landings", "Landings", start_page=201),
        *page_197,
    ]
    titles = [s.title for s in sort_skeletons(mixed)]
    assert titles == [
        "Non-fire-isolated stairways and ramps",
        "Separation of rising and descending stair flights",
        "Open access ramps and balconies",
        "Smoke lobbies",
        "Landings",
    ]
    # Alphabetical would put Open before Separation — must not happen
    assert titles.index("Separation of rising and descending stair flights") < titles.index(
        "Open access ramps and balconies"
    )


def test_hierarchy_tree_matches_node_list_order() -> None:
    base = "doc.pdf/Part D3"
    skeletons = [
        _skel(
            f"{base}/Separation of rising and descending stair flights",
            "Separation of rising and descending stair flights",
            start_page=197,
        ),
        _skel(
            f"{base}/Open access ramps and balconies",
            "Open access ramps and balconies",
            start_page=197,
        ),
    ]
    tree = build_hierarchy_tree(skeletons)
    assert list(tree["Part D3"].keys()) == [
        "Separation of rising and descending stair flights",
        "Open access ramps and balconies",
    ]

    artifact = serialize_hierarchy_artifact(skeletons)
    assert list(artifact["HIERARCHY"]["Part D3"].keys()) == [
        n["title"] for n in artifact["nodes"]
    ]
    assert [n["title"] for n in artifact["nodes"]] == [
        "Separation of rising and descending stair flights",
        "Open access ramps and balconies",
    ]
