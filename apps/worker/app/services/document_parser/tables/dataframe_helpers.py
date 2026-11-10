from __future__ import annotations

import os

import pandas as pd


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
