"""Filename safety helpers for parser working files."""

import os
from hashlib import sha256


MAX_INTERNAL_FILENAME_BYTES = 240
INTERNAL_FILENAME_HASH_LENGTH = 12


def truncate_internal_filename(filename: str) -> str:
    """Keep parser filenames below Linux NAME_MAX while retaining identity."""
    if len(os.fsencode(filename)) <= MAX_INTERNAL_FILENAME_BYTES:
        return filename

    name_root, name_ext = os.path.splitext(filename)
    filename_hash = sha256(filename.encode("utf-8")).hexdigest()[
        :INTERNAL_FILENAME_HASH_LENGTH
    ]
    suffix = f"-{filename_hash}{name_ext}"
    available_root_bytes = MAX_INTERNAL_FILENAME_BYTES - len(os.fsencode(suffix))
    if available_root_bytes <= 0:
        return f"document-{filename_hash}.bin"

    truncated_root = name_root.encode("utf-8")[:available_root_bytes].decode(
        "utf-8", errors="ignore"
    )
    return f"{truncated_root}{suffix}"
