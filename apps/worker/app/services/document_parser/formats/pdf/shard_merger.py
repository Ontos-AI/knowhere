"""Merge MinerU image outputs from multiple shards into a single unified output."""

from __future__ import annotations

import os
import shutil

from loguru import logger


def merge_images(shard_dirs: list[str], target_dir: str) -> None:
    """Copy all images from shard images/ dirs into target_dir/images/."""
    target_img_dir = os.path.join(target_dir, "images")
    os.makedirs(target_img_dir, exist_ok=True)
    total = 0
    for shard_dir in shard_dirs:
        if shard_dir is None:
            continue
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
