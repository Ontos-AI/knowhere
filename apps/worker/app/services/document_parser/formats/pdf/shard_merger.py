"""Merge MinerU image outputs from multiple shards into a single unified output."""

from __future__ import annotations

import os
import re
import shutil

from loguru import logger


def _extract_heading_key(line: str) -> tuple[int, str] | None:
    """Return (level, text) if *line* is a Markdown heading, else None."""
    m = re.match(r"^(#+)\s*(.*)", line)
    if not m:
        return None
    return len(m.group(1)), m.group(2).strip()


def merge_shard_lines(shard_lines_list: list[list[str]]) -> list[str]:
    """Concatenate per-shard lines_with_heading in order, removing boundary duplicates.

    When a PDF section-divider page falls at the end of shard N and the same
    heading opens shard N+1, each shard independently identifies it as a heading,
    resulting in two consecutive identical headings after naïve concatenation.

    Strategy: before appending shard N's lines, if the *last heading* in that
    shard's output has the same (level, text) as the *first heading* in shard
    N+1's output, strip the trailing heading (and any non-heading lines that
    follow it, i.e. the divider page content) from shard N's lines.
    """
    if not shard_lines_list:
        return []

    result: list[str] = []
    for shard_idx, lines in enumerate(shard_lines_list):
        if not lines:
            continue

        # Determine next shard's first heading (if any)
        next_first_heading: tuple[int, str] | None = None
        for future_idx in range(shard_idx + 1, len(shard_lines_list)):
            for next_line in shard_lines_list[future_idx]:
                key = _extract_heading_key(next_line)
                if key is not None:
                    next_first_heading = key
                    break
            if next_first_heading is not None:
                break

        # Find this shard's last heading and its position
        lines_to_add = list(lines)
        if next_first_heading is not None:
            last_heading_pos: int | None = None
            last_heading_key: tuple[int, str] | None = None
            for pos, line in enumerate(lines_to_add):
                key = _extract_heading_key(line)
                if key is not None:
                    last_heading_pos = pos
                    last_heading_key = key

            if (
                last_heading_pos is not None
                and last_heading_key is not None
                and last_heading_key == next_first_heading
            ):
                # Truncate from the last (duplicate) heading onward
                logger.info(
                    f"🔗 shard_{shard_idx}: removing trailing boundary heading "
                    f"'{last_heading_key[1]}' (L{last_heading_key[0]}) duplicated "
                    f"at start of next shard ({len(lines_to_add) - last_heading_pos} lines trimmed)"
                )
                lines_to_add = lines_to_add[:last_heading_pos]

        result.extend(lines_to_add)

    return result


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
