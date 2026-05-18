from __future__ import annotations

import os
import re

import pandas as pd
from app.services.document_parser.assets.image_compressor import (
    apply_rename_map_to_dataframe,
    compress_output_images,
)
from app.services.document_parser.support.stage_profiler import stage_timer
from loguru import logger


def apply_parse_postprocess(
    output_dir: str,
    parsed_df: pd.DataFrame | None,
) -> pd.DataFrame | None:
    """Apply output cleanup and image compression after parsing."""
    logger.debug(f"full_output_dir: {output_dir}")

    with stage_timer("document.cleanup_unreferenced_images", output_dir=output_dir):
        cleanup_unreferenced_images(output_dir)

    with stage_timer("document.compress_images", output_dir=output_dir):
        compress_stats = compress_output_images(output_dir)
        if compress_stats.processed > 0:
            logger.info(
                f"📦 Image compression: {compress_stats.processed} processed "
                f"({compress_stats.converted_png_to_jpg} PNG→JPG, "
                f"{compress_stats.resized} resized), "
                f"{compress_stats.bytes_before / 1024 / 1024:.1f}MB → "
                f"{compress_stats.bytes_after / 1024 / 1024:.1f}MB"
            )
        if compress_stats.rename_map and parsed_df is not None:
            return apply_rename_map_to_dataframe(parsed_df, compress_stats.rename_map)

    return parsed_df


def cleanup_unreferenced_images(output_dir: str) -> int:
    """Remove UUID-named images that are not referenced by final parsed output."""
    image_dir = os.path.join(output_dir, "images")
    if not os.path.isdir(image_dir):
        return 0

    uuid_pattern = re.compile(
        r"^[a-f0-9]{64}\.(?:jpg|jpeg|png|gif|webp)$",
        re.IGNORECASE,
    )
    removed_count = 0

    for filename in os.listdir(image_dir):
        if not uuid_pattern.match(filename):
            continue

        file_path = os.path.join(image_dir, filename)
        try:
            os.remove(file_path)
            removed_count += 1
            logger.debug(f"Removed unreferenced image: {filename}")
        except OSError as exc:
            logger.warning(f"Failed to remove {filename}: {exc}")

    if removed_count > 0:
        logger.info(
            f"Cleaned up {removed_count} unreferenced UUID-named images from {image_dir}"
        )

    return removed_count
