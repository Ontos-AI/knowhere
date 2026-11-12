from __future__ import annotations

import shutil
from dataclasses import dataclass

from shared.core.exceptions.domain_exceptions import DependencyMissingException


@dataclass(frozen=True)
class MediaToolchain:
    ffmpeg_path: str
    ffprobe_path: str


def require_media_toolchain() -> MediaToolchain:
    """Resolve ffmpeg and ffprobe, or fail with a clear missing-dependency error."""
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path is None or ffprobe_path is None:
        names = " and ".join(
            name
            for name, resolved in (("ffmpeg", ffmpeg_path), ("ffprobe", ffprobe_path))
            if resolved is None
        )
        raise DependencyMissingException(
            internal_message=(
                f"{names} not found on PATH. "
                "Install ffmpeg in the worker image or on the host before parsing media."
            ),
            user_message="Media parsing is unavailable because ffmpeg is not installed.",
        )
    return MediaToolchain(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path)
