"""Convert a persisted original image into temporary PNG frames for VLM calls."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from shared.core.exceptions.domain_exceptions import ImageParsingException

# Agreed parse-time sample cap for multi-frame sources.
MAX_VLM_FRAMES = 20

_HEIF_SUFFIXES = {".heic", ".heif"}
_SEQUENCE_KIND_BY_SUFFIX = {
    ".gif": "gif",
    ".webp": "animated_webp",
    ".tif": "tiff_pages",
    ".tiff": "tiff_pages",
}


@dataclass(frozen=True)
class VlmImageInput:
    working_paths: tuple[str, ...]
    work_dir: str
    source_kind: str
    frame_count: int

    def prompt_paras(self) -> dict[str, object]:
        if self.frame_count <= 1:
            return {}
        return {
            "sequence_kind": self.source_kind,
            "sequence_frames": self.frame_count,
        }

    def cleanup(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)


def sample_frame_indices(
    frame_count: int,
    max_frames: int = MAX_VLM_FRAMES,
) -> list[int]:
    """Return frame indexes, including first and last when sampling down."""
    if frame_count <= 0:
        return []
    if frame_count <= max_frames:
        return list(range(frame_count))
    return [
        round(index * (frame_count - 1) / (max_frames - 1))
        for index in range(max_frames)
    ]


def prepare_vlm_image_input(*, source_path: str, filename: str) -> VlmImageInput:
    """Open the original file and write sampled PNG frames outside ``images/``."""
    suffix = Path(filename).suffix.lower()
    if suffix in _HEIF_SUFFIXES:
        _register_heif_opener()

    try:
        image = Image.open(source_path)
    except Exception as exc:
        raise ImageParsingException(
            user_message="Failed to process the image file",
            reason="IMAGE_DECODE_FAILED",
            internal_message=f"Failed to decode image: {exc}",
            original_exception=exc,
        ) from exc

    work_dir = tempfile.mkdtemp(prefix="vlm_image_")
    try:
        total_frames = getattr(image, "n_frames", 1) or 1
        indexes = sample_frame_indices(total_frames)
        working_paths: list[str] = []
        for output_index, frame_index in enumerate(indexes):
            image.seek(frame_index)
            frame = image.convert("RGBA")
            output_path = os.path.join(work_dir, f"frame-{output_index:03d}.png")
            frame.save(output_path, "PNG")
            working_paths.append(output_path)
    except Exception as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise ImageParsingException(
            user_message="Failed to process the image file",
            reason="IMAGE_DECODE_FAILED",
            internal_message=f"Failed to prepare VLM image frames: {exc}",
            original_exception=exc,
        ) from exc
    finally:
        image.close()

    source_kind = "static"
    if len(working_paths) > 1:
        source_kind = _SEQUENCE_KIND_BY_SUFFIX.get(suffix, "static")

    return VlmImageInput(
        working_paths=tuple(working_paths),
        work_dir=work_dir,
        source_kind=source_kind,
        frame_count=len(working_paths),
    )


def _register_heif_opener() -> None:
    try:
        from pillow_heif import register_heif_opener
    except ImportError as exc:
        raise ImageParsingException(
            user_message="Failed to process the image file",
            reason="IMAGE_DECODE_FAILED",
            internal_message="pillow-heif is required to decode HEIC/HEIF images",
            original_exception=exc,
        ) from exc
    register_heif_opener()
