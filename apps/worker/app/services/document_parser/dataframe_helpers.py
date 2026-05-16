from __future__ import annotations

import os
from typing import Any

import pandas as pd

from app.services.document_parser.identifiers import gen_str_codes


def flatten_list(nested_list: list[Any]) -> list[Any]:
    """Flatten a nested list."""
    flat_list: list[Any] = []
    for item in nested_list:
        if isinstance(item, list):
            flat_list.extend(flatten_list(item))
        else:
            flat_list.append(item)
    return flat_list


def merge_df(input_df: pd.DataFrame) -> pd.DataFrame:
    """Merge DataFrame rows that share the same path."""
    dfs_by_path = list(input_df.groupby("path", sort=False))
    processed_dfs: list[pd.DataFrame] = []

    for key, df in dfs_by_path:
        content_to_merge: list[str] = []
        types_to_merge: list[str] = []
        total_length = 0

        for _, row in df.iterrows():
            content_to_merge.append(str(row["content"]))
            types_to_merge.extend(str(row["type"]).split("\n"))
            total_length += len(str(row["content"]))

        processed_dfs.append(
            pd.DataFrame(
                [
                    {
                        "content": "\n".join(content_to_merge),
                        "type": "\n".join(sorted(set(types_to_merge))),
                        "path": key,
                        "length": total_length,
                        "know_id": gen_str_codes("\n".join(content_to_merge)),
                    }
                ]
            )
        )

    return pd.concat(processed_dfs, axis=0, ignore_index=True)


def process_dup_paths_df(df: pd.DataFrame) -> pd.DataFrame:
    """De-duplicate KB DataFrame paths for final output."""
    if "path" not in df.columns:
        return df

    split_char = os.getenv("SPLIT_CHAR", "/")
    dup_mask = df["path"].duplicated(keep=False)
    if not dup_mask.any():
        return df

    path_occurrences: dict[str, list[int]] = {}
    for idx, path in enumerate(df["path"]):
        path_occurrences.setdefault(str(path), []).append(idx)

    path_renames: dict[int, str] = {}
    parent_rename_map: dict[str, dict[int, str]] = {}

    for path, indices in path_occurrences.items():
        if len(indices) > 1:
            parent_rename_map[path] = {}
            for occurrence, idx in enumerate(indices):
                if occurrence == 0:
                    path_renames[idx] = path
                else:
                    new_path = f"{path}_{occurrence + 1}"
                    path_renames[idx] = new_path
                    parent_rename_map[path][idx] = new_path

    new_paths: list[str] = []
    for idx, row in df.iterrows():
        row_index = int(str(idx))
        path = str(row["path"])
        new_path = path_renames.get(row_index, path)
        path_parts = new_path.split(split_char)

        for parent_path, rename_info in parent_rename_map.items():
            parent_parts = parent_path.split(split_char)
            if len(path_parts) > len(parent_parts) and path_parts[: len(parent_parts)] == parent_parts:
                matching_parent_idx = None
                for parent_idx in sorted(rename_info.keys(), reverse=True):
                    if parent_idx < row_index:
                        matching_parent_idx = parent_idx
                        break
                if matching_parent_idx is not None:
                    renamed_parent = rename_info[matching_parent_idx]
                    renamed_parent_parts = renamed_parent.split(split_char)
                    new_path = split_char.join(
                        renamed_parent_parts + path_parts[len(parent_parts) :]
                    )
                    break

        new_paths.append(new_path)

    df = df.copy()
    df["path"] = new_paths
    return df
