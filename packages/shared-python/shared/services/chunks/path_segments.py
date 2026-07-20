"""Escape semantic slashes in document hierarchy path segments.

Parser chunk paths use ``/`` as a deterministic hierarchy separator
(``file.pdf/Section/Subsection``). Heading titles may themselves contain
``/`` (e.g. ``Symbols/Numbers``). Those semantic slashes must be escaped
before join and unescaped after split so they are not treated as levels.

The escape character is U+2215 DIVISION SLASH (``∕``), matching the existing
markdown parse-state behavior.
"""

from __future__ import annotations

# Deterministic hierarchy separator used in parser chunk / section paths.
DOCUMENT_PATH_SEP = "/"

# Replacement for semantic ``/`` inside a single path segment.
# Must not equal DOCUMENT_PATH_SEP and should be rare in natural titles.
ESCAPED_DOCUMENT_PATH_SEP = "\u2215"  # ∕


def escape_path_segment(segment: object) -> str:
    """Escape separator characters inside one hierarchy title/segment."""
    text = str(segment or "")
    if not text:
        return ""
    # Escape the escape character first so round-trips stay injective.
    text = text.replace(ESCAPED_DOCUMENT_PATH_SEP, ESCAPED_DOCUMENT_PATH_SEP * 2)
    return text.replace(DOCUMENT_PATH_SEP, ESCAPED_DOCUMENT_PATH_SEP)


def unescape_path_segment(segment: object) -> str:
    """Restore semantic slashes previously escaped by ``escape_path_segment``."""
    text = str(segment or "")
    if not text:
        return ""
    out: list[str] = []
    i = 0
    esc = ESCAPED_DOCUMENT_PATH_SEP
    while i < len(text):
        if text.startswith(esc + esc, i):
            out.append(esc)
            i += 2
            continue
        if text.startswith(esc, i):
            out.append(DOCUMENT_PATH_SEP)
            i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def join_document_path(parts: list[object] | tuple[object, ...]) -> str:
    """Join hierarchy parts with ``/``, escaping each part first."""
    escaped = [escape_path_segment(part) for part in parts if str(part or "")]
    return DOCUMENT_PATH_SEP.join(escaped)


def append_document_path(parent_path: object, *segments: object) -> str:
    """Append title segments under an already-escaped parent path."""
    parent = str(parent_path or "")
    extras = [escape_path_segment(segment) for segment in segments if str(segment or "")]
    if not parent:
        return DOCUMENT_PATH_SEP.join(extras)
    if not extras:
        return parent
    return DOCUMENT_PATH_SEP.join([parent, *extras])


def split_escaped_document_path(path: object) -> list[str]:
    """Split a ``/``-joined hierarchy path and unescape each segment."""
    raw = str(path or "").strip()
    if not raw:
        return []
    return [
        unescape_path_segment(part)
        for part in raw.split(DOCUMENT_PATH_SEP)
        if part != ""
    ]
