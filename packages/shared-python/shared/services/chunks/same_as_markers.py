"""Shared SAME-AS marker helpers for page-memory retrieval."""

from __future__ import annotations

import re

SAME_AS_PREFIX = "SAME-AS"
SAME_AS_MARKER_RE = re.compile(rf"\[{SAME_AS_PREFIX} [^\]]+\]")
SAME_AS_RELATION = "same_as"
MEDIA_RELATIONS = frozenset({"embeds", "related"})


def strip_same_as_markers(text: str) -> str:
    """Remove ``[SAME-AS ...]`` markers from display/search text."""
    return SAME_AS_MARKER_RE.sub("", str(text or "")).strip()


def contains_same_as_marker(text: str) -> bool:
    return bool(SAME_AS_MARKER_RE.search(str(text or "")))
