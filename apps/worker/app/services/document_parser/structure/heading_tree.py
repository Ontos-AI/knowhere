from __future__ import annotations

import pandas as pd
from loguru import logger


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
        current_path = (
            f"{parent_path}/{tree_node_key}" if parent_path else tree_node_key
        )
        stack.append((level, parent_dict[tree_node_key], tree_node_key, current_path))

    return root, node_to_id, id_to_row


def tree_to_dataframe(
    tree: dict[str, dict],
    node_to_id: dict[tuple[str, str], int],
    original_df: pd.DataFrame,
) -> pd.DataFrame:
    preserved_headings = _extract_headings_from_tree(tree, node_to_id)
    preserved_ids = {heading["id"] for heading in preserved_headings}

    updated_df = original_df.copy()
    removed_count = 0
    level_changed_count = 0

    for index, row in original_df.iterrows():
        row_id = int(row["id"])
        old_level = int(row["level"]) if row["level"] not in [-2, "nan", -1] else -1

        if old_level <= -1:
            continue

        if row_id in preserved_ids:
            new_level = next(
                (
                    heading["level"]
                    for heading in preserved_headings
                    if heading["id"] == row_id
                ),
                old_level,
            )
            updated_df.at[index, "level"] = new_level
            if new_level != old_level:
                level_changed_count += 1
        else:
            updated_df.at[index, "level"] = -1
            removed_count += 1

    logger.debug(
        f"Tree changed: removed headings={removed_count}, "
        f"level changed={level_changed_count}, preserved headings={len(preserved_ids)}"
    )
    return updated_df


def remove_isolated_nodes(tree: dict[str, dict]) -> dict[str, dict]:
    return _remove_isolated_nodes_recursive(tree)


def cleanup_heading_tree(heading_preds: pd.DataFrame) -> pd.DataFrame:
    if heading_preds.empty:
        return heading_preds

    tree, node_to_id, _ = build_tree_from_dataframe(heading_preds)
    processed_tree = remove_isolated_nodes(tree)
    return tree_to_dataframe(processed_tree, node_to_id, heading_preds)


def _extract_headings_from_tree(
    node_dict: dict[str, dict],
    node_to_id: dict[tuple[str, str], int],
    *,
    current_level: int = 1,
    parent_path: str = "",
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for tree_node_key, children in node_dict.items():
        node_key = (tree_node_key, parent_path)
        row_id = node_to_id.get(node_key, -1)

        if row_id >= 0:
            original_heading = (
                tree_node_key.split("#")[0] if "#" in tree_node_key else tree_node_key
            )
            results.append(
                {
                    "id": row_id,
                    "heading": original_heading,
                    "level": current_level,
                    "tree_key": tree_node_key,
                    "parent_path": parent_path,
                }
            )

            if isinstance(children, dict) and children:
                current_path = (
                    f"{parent_path}/{tree_node_key}" if parent_path else tree_node_key
                )
                results.extend(
                    _extract_headings_from_tree(
                        children,
                        node_to_id,
                        current_level=current_level + 1,
                        parent_path=current_path,
                    )
                )
    return results


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
                    parent_path=f"{parent_path}/{heading}" if parent_path else heading,
                )
        elif isinstance(children, dict) and children:
            result_dict[heading] = _remove_isolated_nodes_recursive(
                children,
                parent_path=f"{parent_path}/{heading}" if parent_path else heading,
            )
        else:
            result_dict[heading] = children

    return result_dict
