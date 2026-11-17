"""Deliver a page's video and audio with the page.

A page row lists the video and audio on its pages under ``metadata.assets``
and names each with one marker line in its content. Evidence replaces that
line, where it stands, with the marker, the media's signed link and its
transcript. Structured results follow the page row with the media rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shared.services.chunks.asset_descriptor import (
    EXTRACTED_ASSET_KEY,
    PAGE_MEDIA_ASSETS_KEY,
    normalize_page_media_asset,
    normalize_page_media_assets,
    page_media_marker,
)


def media_display(media_row: Mapping[str, Any]) -> str:
    """A video or audio row as marker line, signed link, then transcript."""
    metadata = media_row.get("chunk_metadata") or media_row.get("metadata") or {}
    asset = (
        normalize_page_media_asset(metadata.get(EXTRACTED_ASSET_KEY))
        if isinstance(metadata, Mapping)
        else None
    )
    content = str(media_row.get("content") or "").strip()
    if asset is None:
        return content
    marker = page_media_marker(asset)
    transcript = content.removeprefix(marker).strip()
    link = str(media_row.get("asset_url") or "").strip()
    return "\n".join(part for part in (marker, link, transcript) if part)


def page_media_rows(
    row: Mapping[str, Any],
    rows_by_chunk_id: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """The loaded media rows of the video and audio this page row carries."""
    metadata = row.get("chunk_metadata") or row.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        return []
    return [
        media_row
        for asset in normalize_page_media_assets(metadata.get(PAGE_MEDIA_ASSETS_KEY))
        if (media_row := rows_by_chunk_id.get(asset["asset_id"])) is not None
    ]


def inline_page_media(
    text: str,
    row: Mapping[str, Any],
    rows_by_chunk_id: Mapping[str, dict[str, Any]],
) -> str:
    """Replace each loaded media marker in ``text`` with marker, link and transcript."""
    metadata = row.get("chunk_metadata") or row.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        return text
    for asset in normalize_page_media_assets(metadata.get(PAGE_MEDIA_ASSETS_KEY)):
        media_row = rows_by_chunk_id.get(asset["asset_id"])
        marker = page_media_marker(asset)
        if media_row is None or marker not in text:
            continue
        text = text.replace(marker, media_display(media_row), 1)
    return text
