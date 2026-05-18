"""General file utilities shared across multiple services."""

import os

import pandas as pd


def clean_file(path_, mode="remove", cols=None):
    """
    Clean or remove a file.

    Args:
        path_: File path.
        mode: Cleanup mode ('remove' deletes the file, 'clean' clears content).
        cols: Column names used when recreating a CSV.
    """
    try:
        if mode == "remove":
            os.remove(path_)
        elif mode == "clean":
            if ".txt" in path_:
                pass
            elif ".csv" in path_:
                exist_df = pd.read_csv(path_, encoding="utf-8", keep_default_na=False)
                if cols is not None:
                    empty_df = pd.DataFrame(columns=cols)
                else:
                    empty_df = pd.DataFrame(columns=exist_df.columns)
                empty_df.to_csv(path_, index=False)
        else:
            pass
    except Exception:
        pass


def path_handle(path, mode):
    """
    Path handling helper.

    Args:
        path: Path string.
        mode: Handling mode ('split', 'extract-base', 'sanitize', 'clean_single').

    Returns:
        Processed path or path list.
    """
    import re

    illegal_chars = r'[\t\n<>：:;；"　/\\|?*]'
    safe_char = "_"

    if mode == "split":
        path_lst = path.split(os.sep)
        return path_lst

    elif mode == "extract-base":
        base_name = os.path.basename(path)
        base_name = os.path.splitext(base_name)[0]
        return base_name

    elif mode == "sanitize":
        path = path.replace("\\", "/")
        parts = path.split("/")
        sanitized_parts = []
        for part in parts:  # Replace illegal characters with a safe placeholder.
            clean_part = re.sub(illegal_chars, safe_char, part)
            sanitized_parts.append(clean_part)
        return os.sep.join(sanitized_parts)

    elif mode == "clean_single":
        path = re.sub(illegal_chars, safe_char, path)
        return path
    return None
