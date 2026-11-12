from __future__ import annotations

import os
import subprocess

from app.services.document_parser.formats.media.toolchain import require_media_toolchain
from app.services.document_parser.support.parser_log_utils import truncate_log_value

from shared.core.exceptions.domain_exceptions import ValidationException

# Working copy name, same role as excel `source_workbook.xlsx`.
SOURCE_AUDIO_REL = "source_audio.wav"
# Fun-ASR native acoustic sample rate (16 kHz mono 16-bit PCM).
ASR_SAMPLE_RATE_HZ = 16000


def extract_source_audio(file_path: str, output_dir: str) -> str:
    """Downmix and resample the admitted media file to 16 kHz mono PCM wav."""
    toolchain = require_media_toolchain()
    os.makedirs(output_dir, exist_ok=True)
    destination = os.path.join(output_dir, SOURCE_AUDIO_REL)
    extract_result = subprocess.run(
        [
            toolchain.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            file_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(ASR_SAMPLE_RATE_HZ),
            "-c:a",
            "pcm_s16le",
            destination,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if extract_result.returncode != 0:
        raise ValidationException(
            user_message="Could not extract audio from this media file.",
            violations=[
                {
                    "field": "file",
                    "description": "ffmpeg could not extract a mono audio track",
                }
            ],
            internal_message=(
                "ffmpeg extract failed: "
                f"input={file_path}, output={destination}, "
                f"exit_code={extract_result.returncode}, "
                f"stdout={truncate_log_value(extract_result.stdout)}, "
                f"stderr={truncate_log_value(extract_result.stderr)}"
            ),
        )
    if not os.path.isfile(destination) or os.path.getsize(destination) <= 0:
        raise ValidationException(
            user_message="Could not extract audio from this media file.",
            violations=[
                {
                    "field": "file",
                    "description": "ffmpeg produced an empty mono audio track",
                }
            ],
            internal_message=(
                "ffmpeg extract wrote no audio bytes: "
                f"input={file_path}, output={destination}"
            ),
        )
    return destination
