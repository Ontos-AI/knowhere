"""Document path parsing for parser chunk paths."""

from __future__ import annotations

import os

_DOCUMENT_FILE_EXTENSIONS = {
    ".csv",
    ".atlas",
    ".fragment",
    ".gif",
    ".doc",
    ".docx",
    ".htm",
    ".html",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".markdown",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rtf",
    ".webp",
    ".txt",
    ".xls",
    ".xlsm",
    ".xlsx",
}
_MEDIA_ROOT_SEGMENTS = {"images", "tables"}


def split_document_path(
    path: str | None,
    *,
    source_file_name: str | None = None,
) -> tuple[list[str], list[str]]:
    """Return ``(root_parts, section_parts)`` for new and legacy chunk paths."""
    parts = _split_path(path, source_file_name=source_file_name)
    if not parts:
        return [], []
    if parts[0] in _MEDIA_ROOT_SEGMENTS and not _is_legacy_namespace_path(
        parts,
        source_file_name=source_file_name,
    ):
        return parts[:1], []

    document_index = _find_document_index(parts, source_file_name=source_file_name)
    return parts[: document_index + 1], parts[document_index + 1 :]


def _split_path(path: str | None, *, source_file_name: str | None) -> list[str]:
    raw = str(path or "").strip()
    raw_segments = raw.split("/")
    source_segment = _normalize_document_file_name(source_file_name)
    parts: list[str] = []
    for index, segment in enumerate(raw_segments):
        parts.extend(
            _split_arrow_document_segment(
                segment,
                can_split=_can_split_arrow_document_segment(
                    index=index,
                    raw_segments=raw_segments,
                    segment=segment,
                    source_segment=source_segment,
                ),
            )
        )
    return parts


def _can_split_arrow_document_segment(
    *,
    index: int,
    raw_segments: list[str],
    segment: str,
    source_segment: str,
) -> bool:
    if index == 0:
        return True
    if index != 1:
        return False

    first_segment = raw_segments[0].strip() if raw_segments else ""
    if not _is_document_file_segment(first_segment):
        return True

    arrow_document_segment = _normalize_document_file_name(
        segment.split("-->", 1)[0]
    )
    return bool(source_segment and arrow_document_segment == source_segment)


def _split_arrow_document_segment(segment: str, *, can_split: bool) -> list[str]:
    normalized_segment = segment.strip()
    if not normalized_segment:
        return []
    if not can_split or "-->" not in normalized_segment:
        return [normalized_segment]

    arrow_parts = [
        part.strip()
        for part in normalized_segment.split("-->")
        if part.strip()
    ]
    if arrow_parts and _is_document_file_segment(arrow_parts[0]):
        return arrow_parts
    return [normalized_segment]


def _find_document_index(
    parts: list[str],
    *,
    source_file_name: str | None,
) -> int:
    source_segment = _normalize_document_file_name(source_file_name)
    if source_segment:
        for index in range(min(2, len(parts))):
            if _normalize_document_file_name(parts[index]) == source_segment:
                return index

    if _is_legacy_namespace_path(parts, source_file_name=source_file_name):
        return 1
    if _is_document_file_segment(parts[0]):
        return 0
    return 0


def _is_legacy_namespace_path(
    parts: list[str],
    *,
    source_file_name: str | None,
) -> bool:
    if len(parts) < 3 or not _is_document_file_segment(parts[1]):
        return False

    source_segment = _normalize_document_file_name(source_file_name)
    if source_segment:
        return _normalize_document_file_name(parts[1]) == source_segment
    return not _is_document_file_segment(parts[0])


def _normalize_document_file_name(value: str | None) -> str:
    if not value:
        return ""
    return os.path.basename(str(value).strip().replace("\\", "/")).lower()


def _is_document_file_segment(segment: str) -> bool:
    normalized_segment = segment.lower().strip()
    return any(
        normalized_segment.endswith(extension)
        for extension in _DOCUMENT_FILE_EXTENSIONS
    )
