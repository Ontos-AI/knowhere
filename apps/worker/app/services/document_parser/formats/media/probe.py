from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from typing import Any

from app.services.document_parser.formats.media.toolchain import require_media_toolchain
from app.services.document_parser.support.parser_log_utils import truncate_log_value

from shared.core.exceptions.domain_exceptions import ValidationException

MEDIA_HAS_NO_AUDIO = "MEDIA_HAS_NO_AUDIO"


@dataclass(frozen=True)
class MediaInfo:
    duration_ms: int | None
    has_audio: bool
    codec: str | None
    channels: int | None


def admit_media_source(file_path: str) -> MediaInfo:
    """Probe a media file and reject sources that have no audio track."""
    info = probe_media(file_path)
    if not info.has_audio:
        raise ValidationException(
            user_message=(
                "This media file has no audio track and cannot be transcribed."
            ),
            violations=[
                {
                    "field": "file",
                    "description": MEDIA_HAS_NO_AUDIO,
                }
            ],
            internal_message=MEDIA_HAS_NO_AUDIO,
        )
    return info


def measure_media_duration_ms(file_path: str, info: MediaInfo) -> int:
    """Return the audio duration, reading packets when the container omits it.

    Browser-recorded webm/ogg often has no duration header, so ffprobe returns
    none; a stream-copy pass over the first audio track reports the real end
    time without decoding.
    """
    if info.duration_ms is not None:
        return info.duration_ms
    toolchain = require_media_toolchain()
    copy_result = subprocess.run(
        [
            toolchain.ffmpeg_path,
            "-v",
            "error",
            "-nostats",
            "-i",
            file_path,
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-f",
            "null",
            "-progress",
            "pipe:1",
            "-",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    duration_ms = (
        _last_progress_time_ms(copy_result.stdout)
        if copy_result.returncode == 0
        else None
    )
    if duration_ms is None:
        raise ValidationException(
            user_message="Could not read the duration of this media file.",
            violations=[
                {
                    "field": "file",
                    "description": "media duration could not be determined",
                }
            ],
            internal_message=(
                "media duration unreadable: "
                f"input={file_path}, exit_code={copy_result.returncode}, "
                f"stderr={truncate_log_value(copy_result.stderr)}"
            ),
        )
    return duration_ms


def _last_progress_time_ms(progress_output: str) -> int | None:
    duration_ms: int | None = None
    for line in progress_output.splitlines():
        key, _, value = line.partition("=")
        if key.strip() != "out_time_us":
            continue
        try:
            microseconds = int(value.strip())
        except ValueError:
            continue
        if microseconds >= 0:
            duration_ms = microseconds // 1000
    return duration_ms


def probe_media(file_path: str) -> MediaInfo:
    """Read duration and audio-stream facts from ffprobe JSON."""
    toolchain = require_media_toolchain()
    probe_result = subprocess.run(
        [
            toolchain.ffprobe_path,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            file_path,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if probe_result.returncode != 0:
        raise ValidationException(
            user_message="Could not read this media file.",
            violations=[
                {
                    "field": "file",
                    "description": "ffprobe could not inspect the uploaded media file",
                }
            ],
            internal_message=(
                "ffprobe failed: "
                f"input={file_path}, exit_code={probe_result.returncode}, "
                f"stdout={truncate_log_value(probe_result.stdout)}, "
                f"stderr={truncate_log_value(probe_result.stderr)}"
            ),
        )
    try:
        payload = json.loads(probe_result.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationException(
            user_message="Could not read this media file.",
            violations=[
                {
                    "field": "file",
                    "description": "ffprobe returned invalid media metadata",
                }
            ],
            internal_message=f"ffprobe JSON decode failed for {file_path}: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise ValidationException(
            user_message="Could not read this media file.",
            violations=[
                {
                    "field": "file",
                    "description": "ffprobe returned invalid media metadata",
                }
            ],
            internal_message=f"ffprobe JSON was not an object for {file_path}",
        )
    return media_info_from_ffprobe_payload(payload)


def media_info_from_ffprobe_payload(payload: dict[str, Any]) -> MediaInfo:
    """Map ffprobe JSON to MediaInfo. Exposed for tests that do not run ffprobe."""
    audio_streams = _audio_streams(payload)
    first_audio = audio_streams[0] if audio_streams else {}
    raw_codec = first_audio.get("codec_name")
    raw_channels = first_audio.get("channels")
    return MediaInfo(
        duration_ms=_duration_ms_from_ffprobe_payload(payload),
        has_audio=bool(audio_streams),
        codec=raw_codec if isinstance(raw_codec, str) else None,
        channels=(
            raw_channels
            if isinstance(raw_channels, int) and not isinstance(raw_channels, bool)
            else None
        ),
    )


def _audio_streams(payload: dict[str, Any]) -> list[dict[str, Any]]:
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return []
    audio_streams: list[dict[str, Any]] = []
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == "audio":
            audio_streams.append(stream)
    return audio_streams


def _duration_ms_from_ffprobe_payload(payload: dict[str, Any]) -> int | None:
    fmt = payload.get("format")
    if not isinstance(fmt, dict):
        return None
    raw_duration = fmt.get("duration")
    if raw_duration is None:
        return None
    try:
        seconds = float(raw_duration)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return int(seconds * 1000)
