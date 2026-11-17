"""Per-page text on coarse page rows.

A coarse row covers a whole section skeleton, but retrieval returns only the
pages a query lit: their images, their text and the video or audio on them.
The row therefore keeps each page's text under ``page_texts`` as
``{"page_num", "text"}`` entries in page order. A page shared with an earlier
skeleton carries a ``SAME-AS <owner path> p<page>`` pointer instead of text.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from shared.services.chunks.asset_descriptor import (
    PAGE_MEDIA_ASSETS_KEY,
    normalize_page_media_assets,
    page_media_marker,
)
from shared.services.chunks.chunk_connections import RELATION_EMBEDS

PAGE_TEXTS_KEY: Final = "page_texts"


def narrow_to_pages(metadata: dict[str, Any], pages: Iterable[int]) -> str | None:
    """Keep only ``pages`` in a coarse row's metadata and return their content.

    Text follows page order; each page's media markers follow its text. The
    row's media and their ``embeds`` edges shrink to the media on those pages.
    Returns None for a row without per-page text (M0 and refined rows).
    """
    raw_entries = metadata.get(PAGE_TEXTS_KEY)
    if not isinstance(raw_entries, list):
        return None
    lit = set(pages)
    entries = [
        {"page_num": entry["page_num"], "text": str(entry.get("text") or "")}
        for entry in raw_entries
        if isinstance(entry, dict)
        and isinstance(entry.get("page_num"), int)
        and entry["page_num"] in lit
    ]
    metadata[PAGE_TEXTS_KEY] = entries

    media = normalize_page_media_assets(metadata.get(PAGE_MEDIA_ASSETS_KEY))
    lit_media = [asset for asset in media if lit.intersection(asset["page_nums"])]
    if PAGE_MEDIA_ASSETS_KEY in metadata:
        metadata[PAGE_MEDIA_ASSETS_KEY] = lit_media
    unlit_ids = {asset["asset_id"] for asset in media} - {
        asset["asset_id"] for asset in lit_media
    }
    connections = metadata.get("connect_to")
    if unlit_ids and isinstance(connections, list):
        metadata["connect_to"] = [
            item
            for item in connections
            if not (
                isinstance(item, dict)
                and item.get("relation") == RELATION_EMBEDS
                and item.get("target") in unlit_ids
            )
        ]

    segments: list[str] = []
    placed: set[str] = set()
    for entry in entries:
        markers: list[str] = []
        for asset in lit_media:
            if entry["page_num"] in asset["page_nums"] and asset["asset_id"] not in placed:
                placed.add(asset["asset_id"])
                markers.append(page_media_marker(asset))
        segment = "\n".join(part for part in (entry["text"].strip(), *markers) if part)
        if segment:
            segments.append(segment)
    return "\n\n".join(segments)
