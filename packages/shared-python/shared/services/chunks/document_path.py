"""Document path parsing for parser chunk paths."""

from __future__ import annotations

import os

from shared.services.chunks.path_segments import unescape_path_segment

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
    parts = _split_path(path)
    if not parts:
        return [], []
    if parts[0] in _MEDIA_ROOT_SEGMENTS and not _is_legacy_namespace_path(
        parts,
        source_file_name=source_file_name,
    ):
        return parts[:1], []

    document_index = _find_document_index(parts, source_file_name=source_file_name)
    return parts[: document_index + 1], parts[document_index + 1 :]


def _split_path(path: str | None) -> list[str]:
    raw = str(path or "").strip()
    # Chunk paths use ``/`` as the sole hierarchy separator. Titles that contain
    # a semantic ``/`` are escaped at construction time (``∕``) and restored here
    # so publication/retrieval see one segment per title.
    return [
        unescape_path_segment(segment.strip())
        for segment in raw.split("/")
        if segment.strip()
    ]


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
