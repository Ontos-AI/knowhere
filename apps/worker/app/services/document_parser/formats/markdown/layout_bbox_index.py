"""Index MinerU ``layout.json`` figure regions by image file name.

MinerU writes one ``layout.json`` per parsed PDF. Every figure span
(``image`` / ``chart``) carries its bounding box and the bare ``{hash}.jpg``
name of the extracted image, which is the same name the markdown references
before ``build_markdown_image_asset`` renames the file. This module turns that
into the ``asset_region`` metadata contract shared with the page track.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from loguru import logger

AssetRegion = dict[str, Any]

COORD_SYSTEM_MINERU_LAYOUT = "mineru_layout"

_FIGURE_SPAN_TYPES = frozenset({"image", "chart"})


def load_layout_image_regions(
    layout_json_path: str,
    page_numbers: Sequence[int] | None = None,
) -> dict[str, AssetRegion]:
    """Return ``image file name -> asset_region`` for one ``layout.json``.

    Args:
        layout_json_path: MinerU ``layout.json`` of a single MinerU request.
        page_numbers: Original 1-based page number of each page of that request,
            indexed by the layout's 0-based ``page_idx``. ``None`` means the
            request parsed the whole original PDF, so ``page_idx + 1`` is the
            original page number.

    A missing ``layout.json`` yields an empty index. A figure whose page cannot
    be resolved is skipped (and logged) rather than given a guessed page.
    """
    if not os.path.exists(layout_json_path):
        logger.debug("layout.json not found, image bbox will not be attached")
        return {}

    with open(layout_json_path, "r", encoding="utf-8") as layout_file:
        layout = json.load(layout_file)

    regions: dict[str, AssetRegion] = {}
    for page in layout["pdf_info"]:
        page_idx = page["page_idx"]
        page_width, page_height = page["page_size"]
        for span in _iter_figure_spans(page["para_blocks"]):
            image_name = os.path.basename(span.get("image_path") or "")
            if not image_name:
                continue
            page_num = _resolve_page_num(page_idx, page_numbers)
            if page_num is None:
                logger.warning(
                    "Skip image bbox for {}: page_idx={} is outside the request page map",
                    image_name,
                    page_idx,
                )
                continue
            regions.setdefault(
                image_name,
                {
                    "page_num": page_num,
                    "bbox": list(span["bbox"]),
                    "coord_origin": "top_left",
                    "coord_system": COORD_SYSTEM_MINERU_LAYOUT,
                    "page_width": page_width,
                    "page_height": page_height,
                    # The ZIP/API asset_region contract requires this field.
                    # MinerU crops the figure at its layout bbox, no margin.
                    "crop_margin_px": 0,
                },
            )
    return regions


def load_shard_image_regions(
    shard_output_dirs: Sequence[str | None],
    shard_page_numbers: Sequence[Sequence[int] | None],
) -> dict[str, AssetRegion]:
    """Merge the per-shard indexes of a sharded PDF into one index.

    ``shard_page_numbers[i]`` is the page map of ``shard_output_dirs[i]`` (see
    ``load_layout_image_regions``). When the same image file name appears in
    several shards, the earliest shard wins, matching document order.
    """
    merged: dict[str, AssetRegion] = {}
    for shard_dir, page_numbers in zip(shard_output_dirs, shard_page_numbers):
        if shard_dir is None:
            continue
        shard_regions = load_layout_image_regions(
            os.path.join(shard_dir, "layout.json"), page_numbers
        )
        for image_name, region in shard_regions.items():
            merged.setdefault(image_name, region)
    return merged


def image_region_metadata(
    image_regions: Mapping[str, AssetRegion],
    image_path: str,
) -> dict[str, AssetRegion] | None:
    """``extra_metadata`` for the image row of a markdown image reference."""
    region = image_regions.get(os.path.basename(image_path))
    return {"asset_region": region} if region is not None else None


def _resolve_page_num(page_idx: int, page_numbers: Sequence[int] | None) -> int | None:
    if page_numbers is None:
        return page_idx + 1
    if 0 <= page_idx < len(page_numbers):
        return page_numbers[page_idx]
    return None


def _iter_figure_spans(blocks: Sequence[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for block in blocks:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["type"] in _FIGURE_SPAN_TYPES:
                    yield span
        yield from _iter_figure_spans(block.get("blocks", []))
