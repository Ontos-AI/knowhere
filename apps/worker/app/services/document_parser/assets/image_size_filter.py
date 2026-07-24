"""Shared minimum image size gate used across parsers.

Discard before rename / VLM summary so undersized icons never enter the
asset pipeline.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from shared.core.constants.processing import ProcessingConstants


def is_below_img_min_size(byte_size: int) -> bool:
    """Return True when ``byte_size`` is under ``IMG_MIN_SIZE`` (10KB)."""
    return byte_size < ProcessingConstants.IMG_MIN_SIZE


def discard_undersized_image_file(
    path: Path | str,
    *,
    label: str = "image",
) -> bool:
    """Delete ``path`` when it exists and is undersized.

    Returns True when the file was discarded (caller should skip rename/LLM).
    Returns False when the file is large enough to keep, or does not exist.
    """
    image_path = Path(path)
    if not image_path.exists():
        return False

    file_size = image_path.stat().st_size
    if not is_below_img_min_size(file_size):
        return False

    logger.debug(
        f"Skipping {label} (too small: {file_size / 1024:.1f} KB): {image_path}"
    )
    try:
        image_path.unlink()
    except OSError as exc:
        logger.debug(f"Failed to remove undersized {label} {image_path}: {exc}")
    return True
