"""Merge MinerU outputs from multiple shards into a single unified output."""

from __future__ import annotations

import json
import os
import shutil
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from app.services.document_parser.formats.pdf.shard_splitter import MergedShard


def merge_shard_outputs(
    shard_output_dirs: list[str],
    shards: list[MergedShard],
    target_output_dir: str,
) -> None:
    """Merge MinerU outputs from all shards into target_output_dir.

    After this call target_output_dir contains:
      - full.md:     concatenated markdown (UUID image refs, no conflicts)
      - layout.json: merged page array with corrected page_idx
      - images/:     all images from all shards
    """
    _merge_full_md(shard_output_dirs, target_output_dir)
    _merge_layout_json(shard_output_dirs, shards, target_output_dir)
    _merge_images(shard_output_dirs, target_output_dir)


def _merge_full_md(shard_dirs: list[str], target_dir: str) -> None:
    target_path = os.path.join(target_dir, "full.md")
    with open(target_path, "w", encoding="utf-8") as out:
        for i, shard_dir in enumerate(shard_dirs):
            md_path = os.path.join(shard_dir, "full.md")
            if not os.path.exists(md_path):
                logger.warning(f"shard {i}: full.md not found at {md_path}")
                continue
            with open(md_path, "r", encoding="utf-8") as f:
                content = f.read()
            if i > 0:
                out.write("\n\n")
            out.write(content)
    logger.info(f"Merged {len(shard_dirs)} full.md files → {target_path}")


def _merge_layout_json(
    shard_dirs: list[str],
    shards: list[MergedShard],
    target_dir: str,
) -> None:
    merged_pages: list[dict] = []
    for shard, shard_dir in zip(shards, shard_dirs):
        layout_path = os.path.join(shard_dir, "layout.json")
        if not os.path.exists(layout_path):
            logger.warning(f"shard {shard.shard_index}: layout.json not found")
            continue
        with open(layout_path, "r", encoding="utf-8") as f:
            layout_data = json.load(f)
        for page in layout_data.get("pdf_info", []):
            page["page_idx"] = page.get("page_idx", 0) + shard.page_offset
            merged_pages.append(page)

    target_path = os.path.join(target_dir, "layout.json")
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump({"pdf_info": merged_pages}, f, ensure_ascii=False)
    logger.info(f"Merged layout.json: {len(merged_pages)} pages → {target_path}")


def _merge_images(shard_dirs: list[str], target_dir: str) -> None:
    target_img_dir = os.path.join(target_dir, "images")
    os.makedirs(target_img_dir, exist_ok=True)
    total = 0
    for shard_dir in shard_dirs:
        img_dir = os.path.join(shard_dir, "images")
        if not os.path.isdir(img_dir):
            continue
        for fname in os.listdir(img_dir):
            src = os.path.join(img_dir, fname)
            dst = os.path.join(target_img_dir, fname)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
                total += 1
    logger.info(f"Merged {total} images → {target_img_dir}")
