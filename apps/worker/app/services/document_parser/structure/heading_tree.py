from __future__ import annotations

from typing import cast

import pandas as pd
from loguru import logger

from shared.services.chunks.path_segments import append_document_path


def build_tree_from_dataframe(
    heading_preds: pd.DataFrame,
) -> tuple[dict[str, dict], dict[tuple[str, str], int], dict[int, dict]]:
    headings = heading_preds[heading_preds["level"] > -1].copy()

    node_to_id: dict[tuple[str, str], int] = {}
    id_to_row: dict[int, dict] = {}
    root: dict[str, dict] = {}
    stack: list[tuple[int, dict, str, str]] = [(0, root, "ROOT", "")]

    for _, row in headings.iterrows():
        heading_text = str(row["heading"])
        row_id = int(row["id"])
        level = int(row["level"])

        id_to_row[row_id] = row.to_dict()

        while len(stack) > 1 and stack[-1][0] >= level:
            stack.pop()

        _, parent_dict, _, parent_path = stack[-1]
        tree_node_key = heading_text
        if tree_node_key in parent_dict:
            tree_node_key = f"{heading_text}#{row_id}"

        node_key = (tree_node_key, parent_path)
        node_to_id[node_key] = row_id

        parent_dict[tree_node_key] = {}
        current_path = append_document_path(parent_path, tree_node_key)
        stack.append((level, parent_dict[tree_node_key], tree_node_key, current_path))

    return root, node_to_id, id_to_row


def tree_to_dataframe(
    tree: dict[str, dict],
    node_to_id: dict[tuple[str, str], int],
    original_df: pd.DataFrame,
) -> pd.DataFrame:
    levels_by_id: dict[int, int] = {}
    _collect_levels_by_id(tree, node_to_id, levels_by_id)

    original_levels: pd.Series = cast(pd.Series, original_df["level"])
    numeric_levels: pd.Series = original_levels.map(
        lambda level: -1 if level == "nan" else int(level)
    )
    heading_rows: pd.Series = numeric_levels.ge(0)
    heading_ids: pd.Series = cast(pd.Series, original_df.loc[heading_rows, "id"]).map(int)
    preserved_levels: pd.Series = heading_ids.map(levels_by_id)
    projected_levels: pd.Series = preserved_levels.fillna(-1).astype(int)

    updated_df: pd.DataFrame = original_df.copy()
    updated_df.loc[heading_rows, "level"] = projected_levels

    removed_count: int = int(preserved_levels.isna().sum())
    changed_levels: pd.Series = projected_levels.ne(numeric_levels.loc[heading_rows])
    level_changed_count: int = int((preserved_levels.notna() & changed_levels).sum())

    logger.debug(
        f"Tree changed: removed headings={removed_count}, "
        f"level changed={level_changed_count}, preserved headings={len(levels_by_id)}"
    )
    return updated_df


def remove_isolated_nodes(tree: dict[str, dict]) -> dict[str, dict]:
    return _remove_isolated_nodes_recursive(tree)


def _collect_levels_by_id(
    node_dict: dict[str, dict],
    node_to_id: dict[tuple[str, str], int],
    levels_by_id: dict[int, int],
    *,
    current_level: int = 1,
    parent_path: str = "",
) -> None:
    for tree_node_key, children in node_dict.items():
        row_id: int = node_to_id.get((tree_node_key, parent_path), -1)
        if row_id < 0:
            continue

        # Preserve the first depth-first match when a row appears in multiple nodes.
        levels_by_id.setdefault(row_id, current_level)
        if isinstance(children, dict) and children:
            _collect_levels_by_id(
                children,
                node_to_id,
                levels_by_id,
                current_level=current_level + 1,
                parent_path=append_document_path(parent_path, tree_node_key),
            )


def _remove_isolated_nodes_recursive(
    node_dict: dict[str, dict],
    *,
    parent_path: str = "",
) -> dict[str, dict]:
    result_dict: dict[str, dict] = {}

    for heading, children in node_dict.items():
        if isinstance(children, dict) and len(children) == 1:
            child_heading = list(children.keys())[0]
            grandchildren = children[child_heading]

            if not grandchildren or (
                isinstance(grandchildren, dict) and len(grandchildren) == 0
            ):
                result_dict[heading] = {}
                logger.debug(
                    f"remove isolated heading: {parent_path}/{heading}/{child_heading}"
                )
            else:
                result_dict[heading] = _remove_isolated_nodes_recursive(
                    children,
                    parent_path=append_document_path(parent_path, heading),
                )
        elif isinstance(children, dict) and children:
            result_dict[heading] = _remove_isolated_nodes_recursive(
                children,
                parent_path=append_document_path(parent_path, heading),
            )
        else:
            result_dict[heading] = children

    return result_dict
