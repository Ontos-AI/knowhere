from __future__ import annotations

from typing import ClassVar

import pandas as pd

from app.services.document_parser.structure.heading_tree import (
    remove_isolated_nodes,
    tree_to_dataframe,
)


class _MeasuredHeadingId(int):
    equality_checks: ClassVar[int] = 0
    __hash__ = int.__hash__

    def __eq__(self, other: object) -> bool:
        type(self).equality_checks += 1
        return super().__eq__(other)


def test_tree_projection_does_not_rescan_headings_for_each_row() -> None:
    heading_count: int = 512
    tree: dict[str, dict[str, object]] = {
        f"Heading {index}": {} for index in range(heading_count)
    }
    node_to_id: dict[tuple[str, str], int] = {
        (heading, ""): _MeasuredHeadingId(index)
        for index, heading in enumerate(tree)
    }
    original: pd.DataFrame = pd.DataFrame(
        {"id": range(heading_count), "level": [2] * heading_count}
    )

    _MeasuredHeadingId.equality_checks = 0
    projected: pd.DataFrame = tree_to_dataframe(tree, node_to_id, original)

    assert projected["level"].tolist() == [1] * heading_count
    assert _MeasuredHeadingId.equality_checks <= 10 * heading_count


def test_tree_projection_preserves_rows_and_uses_first_matching_node() -> None:
    tree: dict[str, dict[str, object]] = {
        "Parent": {"First child": {}, "Second child": {}},
        "Standalone": {"Removed child": {}},
        "Duplicate ID": {},
    }
    node_to_id: dict[tuple[str, str], int] = {
        ("Parent", ""): 100,
        ("First child", "Parent"): 101,
        ("Second child", "Parent"): 102,
        ("Standalone", ""): 103,
        ("Removed child", "Standalone"): 104,
        ("Duplicate ID", ""): 101,
    }
    original: pd.DataFrame = pd.DataFrame(
        {
            "id": [100, 101, 102, 103, 104, 105, 106],
            "level": [3, 4, 4, 2, 3, -1, -2],
            "heading": [
                "Parent",
                "First child",
                "Second child",
                "Standalone",
                "Removed child",
                "Body",
                "Rule candidate",
            ],
        },
        index=[4, 6, 7, 10, 13, 16, 18],
    )
    expected: pd.DataFrame = original.copy()
    expected["level"] = [1, 2, 2, 1, -1, -1, -2]

    projected: pd.DataFrame = tree_to_dataframe(
        remove_isolated_nodes(tree), node_to_id, original
    )

    pd.testing.assert_frame_equal(projected, expected)
    assert original["level"].tolist() == [3, 4, 4, 2, 3, -1, -2]
