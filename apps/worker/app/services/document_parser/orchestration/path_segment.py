from __future__ import annotations

import os

from app.services.common.file_utils import path_handle


def build_parser_path_segment(value: str | None, default: str = "document") -> str:
    """Map parser-owned names to one safe task-local path segment."""
    raw_value = str(value or "").strip()
    raw_segment = os.path.basename(raw_value) if raw_value else default
    sanitized_segment = path_handle(raw_segment, mode="clean_single")
    if not isinstance(sanitized_segment, str):
        return default

    segment = sanitized_segment.strip()
    if segment in {"", ".", ".."}:
        return default
    return segment
