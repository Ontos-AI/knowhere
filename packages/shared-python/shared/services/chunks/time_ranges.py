"""Media ``time_ranges`` (``[{"start_ms", "end_ms"}]``) helpers shared by
publication, agent tools, and the media parser."""

from __future__ import annotations

from typing import Any

_MS_PER_SECOND = 1000
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def format_clock_ms(ms: int) -> str:
    """Render milliseconds as ``hh:mm:ss``."""
    total_seconds = ms // _MS_PER_SECOND
    hours, remainder = divmod(total_seconds, _SECONDS_PER_HOUR)
    minutes, seconds = divmod(remainder, _SECONDS_PER_MINUTE)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def valid_time_ranges(value: object) -> list[dict[str, int]]:
    """Keep the well-formed ``{start_ms, end_ms}`` entries of a metadata value."""
    if not isinstance(value, list):
        return []
    ranges: list[dict[str, int]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        start_ms = item.get("start_ms")
        end_ms = item.get("end_ms")
        if (
            isinstance(start_ms, int)
            and isinstance(end_ms, int)
            and not isinstance(start_ms, bool)
            and not isinstance(end_ms, bool)
            and 0 <= start_ms <= end_ms
        ):
            ranges.append({"start_ms": start_ms, "end_ms": end_ms})
    return ranges


def format_time_ranges(value: object) -> str:
    """Render time ranges as ``[hh:mm:ss–hh:mm:ss]`` labels; empty when none."""
    return " ".join(
        f"[{format_clock_ms(item['start_ms'])}–{format_clock_ms(item['end_ms'])}]"
        for item in valid_time_ranges(value)
    )


def widen_time_span(span: object, value: object) -> list[dict[str, int]]:
    """Return ``span`` (one range or none) widened to cover ``value``'s ranges."""
    ranges = [*valid_time_ranges(span), *valid_time_ranges(value)]
    if not ranges:
        return []
    return [
        {
            "start_ms": min(item["start_ms"] for item in ranges),
            "end_ms": max(item["end_ms"] for item in ranges),
        }
    ]


def section_time_ranges(section_metadata: Any) -> list[dict[str, int]]:
    """The section's media time span stored by publication, if any."""
    if not isinstance(section_metadata, dict):
        return []
    return valid_time_ranges(section_metadata.get("time_ranges"))
