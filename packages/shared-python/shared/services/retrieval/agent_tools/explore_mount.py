"""Shared explore-phase mounting for ``corpus.grep`` and ``corpus.recall`` hits.

Table/image hits are rendered with the same explore table/image functions
``corpus.read`` uses. Body hits that already list ``connect_to`` targets get
those assets rendered and labeled as mounted, not as the body match itself.
"""

from __future__ import annotations

from typing import Any

from shared.services.retrieval.agent_tools.registry import ToolContext
from shared.services.retrieval.hydration.assets import (
    enrich_rows_with_retrieval_asset_url,
)
from shared.services.retrieval.hydration.connected import hydrate_connected_target_rows
from shared.services.retrieval.hydration.result_assembly import _image_display_content
from shared.services.retrieval.hydration.row_utils import (
    iter_connected_target_ids,
    normalize_chunk_type,
)
from shared.services.retrieval.hydration.table_grid import render_explore_table
from shared.services.retrieval.settings import ASSET_CHUNK_TYPES

_BODY_CHUNK_TYPES = ("text", "page")


def _https_image_media(row: dict[str, Any]) -> list[dict[str, str]]:
    if normalize_chunk_type(row.get("chunk_type")) != "image":
        return []
    url = str(row.get("asset_url") or "").strip()
    if url.startswith("https://"):
        return [{"type": "image_url", "url": url}]
    return []


def _append_image_media(
    row: dict[str, Any],
    media: list[dict[str, str]],
    seen: set[str],
) -> None:
    for item in _https_image_media(row):
        url = item["url"]
        if url in seen:
            continue
        seen.add(url)
        media.append(item)


async def mount_explore_hits(
    ctx: ToolContext,
    hits: list[dict[str, Any]],
    *,
    char_budget: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Attach rendered table/image content to already-found hits."""
    if not hits:
        return hits, []

    body_hits = [
        hit
        for hit in hits
        if normalize_chunk_type(hit.get("chunk_type")) in _BODY_CHUNK_TYPES
        and iter_connected_target_ids(hit)
    ]
    connected_rows: list[dict[str, Any]] = []
    if body_hits:
        connected_rows = await hydrate_connected_target_rows(
            db=ctx.db,
            rows=body_hits,
            exclude_document_ids=[],
            document_scope=ctx.document_scope,
            exclude_sections=[],
        )

    render_rows = [
        hit
        for hit in hits
        if normalize_chunk_type(hit.get("chunk_type")) in ASSET_CHUNK_TYPES
    ]
    render_rows.extend(connected_rows)
    by_id: dict[str, dict[str, Any]] = {}
    if render_rows:
        enriched = await enrich_rows_with_retrieval_asset_url(
            render_rows,
            log_context="agent_tools.explore_mount",
        )
        by_id = {
            str(row.get("chunk_id") or ""): row
            for row in enriched
            if row.get("chunk_id")
        }

    media: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    mounted: list[dict[str, Any]] = []
    for hit in hits:
        row = dict(hit)
        chunk_id = str(row.get("chunk_id") or "")
        chunk_type = normalize_chunk_type(row.get("chunk_type"))
        enriched_self = by_id.get(chunk_id, row)
        if chunk_type == "table":
            row["rendered"] = render_explore_table(
                enriched_self, char_budget=char_budget
            )
        elif chunk_type == "image":
            row["rendered"] = _image_display_content(enriched_self)
            _append_image_media(enriched_self, media, seen_urls)
        elif chunk_type in _BODY_CHUNK_TYPES:
            parts: list[str] = []
            for target_id in iter_connected_target_ids(row):
                target = by_id.get(target_id)
                if not target:
                    continue
                target_type = normalize_chunk_type(target.get("chunk_type"))
                if target_type == "table":
                    body = render_explore_table(target, char_budget=char_budget)
                    if body:
                        parts.append(f"mounted table chunk_id={target_id}:\n{body}")
                elif target_type == "image":
                    body = _image_display_content(target)
                    if body:
                        parts.append(f"mounted image chunk_id={target_id}:\n{body}")
                    _append_image_media(target, media, seen_urls)
            row["rendered"] = "\n\n".join(parts)
        mounted.append(row)
    return mounted, media
