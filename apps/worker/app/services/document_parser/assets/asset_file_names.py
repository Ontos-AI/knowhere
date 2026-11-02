"""Unified cosmetic stems for extracted image/table files.

Format: ``image-[n] descriptor`` / ``table-[n] descriptor``.
The numeric prefix is never truncated; only the descriptor is capped.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.services.common.file_utils import MAX_ASSET_FILE_NAME_CHARS, path_handle

ASSET_KIND_IMAGE = "image"
ASSET_KIND_TABLE = "table"

_ASSET_STEM_RE = re.compile(
    r"^(?P<kind>image|table)-\[(?P<index>\d+)\](?:\s+(?P<descriptor>.*))?$"
)
# Leftover debug artifacts from the previous ``image-N`` / ``image-N-`` writers.
_LEGACY_OWNED_IMAGE_RE = re.compile(r"^image-\d+")


def build_asset_index_label(kind: str, index: int) -> str:
    _validate_asset_kind(kind)
    return f"{kind}-[{int(index)}]"


def build_asset_file_stem(
    kind: str,
    index: int,
    descriptor: str = "",
    *,
    max_chars: int = MAX_ASSET_FILE_NAME_CHARS,
) -> str:
    prefix = build_asset_index_label(kind, index)
    cleaned = _clean_asset_descriptor(descriptor)
    if not cleaned:
        return prefix
    room = max_chars - len(prefix) - 1
    if room <= 0:
        return prefix
    if len(cleaned) > room:
        cleaned = cleaned[:room].rstrip()
    if not cleaned:
        return prefix
    return f"{prefix} {cleaned}"


def parse_asset_file_index(name: str, kind: str | None = None) -> int | None:
    stem = Path(os.path.basename(str(name).strip())).stem
    match = _ASSET_STEM_RE.match(stem)
    if match is None:
        return None
    if kind is not None and match.group("kind") != kind:
        return None
    return int(match.group("index"))


def is_owned_image_filename(filename: str) -> bool:
    """True for current ``image-[n]`` files and leftover ``image-N`` files."""
    base = os.path.basename(str(filename))
    return parse_asset_file_index(base, kind=ASSET_KIND_IMAGE) is not None or bool(
        _LEGACY_OWNED_IMAGE_RE.match(base)
    )


def _clean_asset_descriptor(descriptor: str) -> str:
    cleaned = path_handle(str(descriptor).strip(), mode="clean_single")
    if not isinstance(cleaned, str):
        return ""
    return " ".join(cleaned.split())


def _validate_asset_kind(kind: str) -> None:
    if kind not in (ASSET_KIND_IMAGE, ASSET_KIND_TABLE):
        raise ValueError(f"Unsupported asset kind: {kind}")
