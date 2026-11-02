"""Unified cosmetic stems for extracted image/table files.

Format: ``image-[n] descriptor`` / ``table-[n] descriptor``
(or ``table-[t]-image-[n] descriptor`` for DOCX table-cell images, and
``image-[p-n] descriptor`` / ``table-[p-n] descriptor`` for page-memory assets).
The bracketed prefix is never truncated; only the descriptor is capped.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.services.common.file_utils import MAX_ASSET_FILE_NAME_CHARS, path_handle

ASSET_KIND_IMAGE = "image"
ASSET_KIND_TABLE = "table"

_ASSET_STEM_RE = re.compile(
    r"^(?P<kind>image|table)-\[(?P<index>\d+(?:-\d+)?)\](?:\s+(?P<descriptor>.*))?$"
)
# Leftover debug artifacts from the previous ``image-N`` / ``image-N-`` writers.
_LEGACY_OWNED_IMAGE_RE = re.compile(r"^image-\d+")


def build_asset_index_label(kind: str, index: int | str) -> str:
    _validate_asset_kind(kind)
    return f"{kind}-[{index}]"


def build_table_image_index_label(table_index: int | str, image_index: int | str) -> str:
    return (
        f"{build_asset_index_label(ASSET_KIND_TABLE, table_index)}-"
        f"{build_asset_index_label(ASSET_KIND_IMAGE, image_index)}"
    )


def build_asset_file_stem_from_prefix(
    prefix: str,
    descriptor: str = "",
    *,
    max_chars: int = MAX_ASSET_FILE_NAME_CHARS,
) -> str:
    cleaned_prefix = str(prefix).strip()
    if not cleaned_prefix:
        raise ValueError("Asset prefix must not be empty")
    cleaned = _clean_asset_descriptor(descriptor)
    if not cleaned:
        return cleaned_prefix
    room = max_chars - len(cleaned_prefix) - 1
    if room <= 0:
        return cleaned_prefix
    if len(cleaned) > room:
        cleaned = cleaned[:room].rstrip()
    if not cleaned:
        return cleaned_prefix
    return f"{cleaned_prefix} {cleaned}"


def build_asset_file_stem(
    kind: str,
    index: int | str,
    descriptor: str = "",
    *,
    max_chars: int = MAX_ASSET_FILE_NAME_CHARS,
) -> str:
    prefix = build_asset_index_label(kind, index)
    return build_asset_file_stem_from_prefix(
        prefix,
        descriptor,
        max_chars=max_chars,
    )


def build_table_image_file_stem(
    table_index: int | str,
    image_index: int | str,
    descriptor: str = "",
    *,
    max_chars: int = MAX_ASSET_FILE_NAME_CHARS,
) -> str:
    prefix = build_table_image_index_label(table_index, image_index)
    return build_asset_file_stem_from_prefix(
        prefix,
        descriptor,
        max_chars=max_chars,
    )


def build_page_asset_file_stem(
    kind: str,
    page_index: int,
    asset_index: int,
    descriptor: str = "",
    *,
    max_chars: int = MAX_ASSET_FILE_NAME_CHARS,
) -> str:
    return build_asset_file_stem(
        kind,
        f"{int(page_index)}-{int(asset_index)}",
        descriptor,
        max_chars=max_chars,
    )


def parse_asset_file_index(name: str, kind: str | None = None) -> int | None:
    stem = Path(os.path.basename(str(name).strip())).stem
    match = _ASSET_STEM_RE.match(stem)
    if match is None:
        return None
    if kind is not None and match.group("kind") != kind:
        return None
    raw_index = match.group("index")
    if not raw_index.isdigit():
        return None
    return int(raw_index)


def is_owned_image_filename(filename: str) -> bool:
    """True for current ``image-[n]`` files and leftover ``image-N`` files."""
    base = os.path.basename(str(filename))
    stem = Path(base).stem
    match = _ASSET_STEM_RE.match(stem)
    if match is not None and match.group("kind") == ASSET_KIND_IMAGE:
        return True
    return bool(_LEGACY_OWNED_IMAGE_RE.match(base))


def _clean_asset_descriptor(descriptor: str) -> str:
    cleaned = path_handle(str(descriptor).strip(), mode="clean_single")
    if not isinstance(cleaned, str):
        return ""
    return " ".join(cleaned.split())


def _validate_asset_kind(kind: str) -> None:
    if kind not in (ASSET_KIND_IMAGE, ASSET_KIND_TABLE):
        raise ValueError(f"Unsupported asset kind: {kind}")
