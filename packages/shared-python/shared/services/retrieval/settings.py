from __future__ import annotations

CHANNEL_WEIGHT_PATH = 1.0
CHANNEL_WEIGHT_CONTENT = 2.0
INTERNAL_RECALL_K_MULTIPLIER = 2
RRF_K = 60
DEFAULT_TOP_K = 10

VALID_CHUNK_TYPES: set[str] = {"text", "image", "table", "page"}
ASSET_CHUNK_TYPES: set[str] = {"image", "table"}


def normalize_chunk_types(chunk_types: list[str] | set[str] | None) -> set[str] | None:
    """Normalize user-provided chunk_types to a validated set.

    Returns None when all types are allowed (input is None or contains all valid types).
    """
    if chunk_types is None:
        return None
    normalized = {t.strip().lower() for t in chunk_types} & VALID_CHUNK_TYPES
    if not normalized or normalized == VALID_CHUNK_TYPES:
        return None
    return normalized


def resolve_disabled_asset_types(chunk_types: set[str] | None) -> set[str]:
    """Derive which asset types should be disabled from the user's allowed chunk_types.

    Returns the set of asset types (image/table) that the user did NOT include.
    """
    if chunk_types is None:
        return set()
    return ASSET_CHUNK_TYPES - chunk_types
