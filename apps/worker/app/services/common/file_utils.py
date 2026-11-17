"""General file utilities shared across multiple services."""

import os


# Shared cap for cosmetic asset filename stems (images/tables). Keeps OS
# basename limits safe while preserving a short debug-friendly context.
MAX_ASSET_FILE_NAME_CHARS = 80


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
