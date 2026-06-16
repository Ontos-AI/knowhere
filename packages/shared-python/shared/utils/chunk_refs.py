import re
from typing import List


IMAGE_RESOURCE_PATH_REF_PATTERN = r"\[images/[^\]\n]+\]"
LINE_LEADING_PAGE_LABEL_PATTERN = r"(?im)^\s*\[page-\d+\]\s*"
RESOURCE_PATH_REF_PATTERN = r"\[(?:images|tables)/[^\]\n]+\]"
CHUNK_REF_PATTERN = RESOURCE_PATH_REF_PATTERN
REFERENCE_LABEL_PATTERN = r"^\s*(?:image|table)-\d+\s*$"

IMAGE_RESOURCE_PATH_REF_RE = re.compile(IMAGE_RESOURCE_PATH_REF_PATTERN, re.IGNORECASE)
LINE_LEADING_PAGE_LABEL_RE = re.compile(LINE_LEADING_PAGE_LABEL_PATTERN)
RESOURCE_PATH_REF_RE = re.compile(RESOURCE_PATH_REF_PATTERN, re.IGNORECASE)
CHUNK_REF_RE = re.compile(CHUNK_REF_PATTERN, re.IGNORECASE)
REFERENCE_LABEL_RE = re.compile(REFERENCE_LABEL_PATTERN, re.IGNORECASE)


def build_chunk_ref(resource_path: str) -> str:
    """Format a chunk reference as a readable path token."""
    normalized_path = str(resource_path or "").strip()
    return f"[{normalized_path}]" if normalized_path else ""


def build_legacy_image_chunk_ref(chunk_id: str) -> str:
    """Format an image chunk reference for legacy consumers."""
    normalized_chunk_id = str(chunk_id or "").strip()
    return f"IMAGE_{normalized_chunk_id}_IMAGE" if normalized_chunk_id else ""


def render_legacy_image_chunk_content(content: str, chunk_id: str) -> str:
    """Render image chunk content with legacy marker and no parser page labels."""
    original_content = str(content or "")
    legacy_ref = build_legacy_image_chunk_ref(chunk_id)
    if not legacy_ref:
        return original_content

    if not IMAGE_RESOURCE_PATH_REF_RE.search(original_content):
        return legacy_ref

    rendered_content = IMAGE_RESOURCE_PATH_REF_RE.sub(legacy_ref, original_content)
    rendered_content = LINE_LEADING_PAGE_LABEL_RE.sub("", rendered_content)
    lines = [line.rstrip() for line in rendered_content.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) if lines else legacy_ref


def extract_chunk_refs(content: str) -> List[str]:
    """Extract all chunk references while preserving order."""
    if not content:
        return []

    refs: List[str] = []
    seen = set()
    for ref in CHUNK_REF_RE.findall(str(content)):
        if ref not in seen:
            refs.append(ref)
            seen.add(ref)
    return refs


def has_chunk_ref(content: str) -> bool:
    """Return whether the content contains a chunk reference."""
    return bool(content and CHUNK_REF_RE.search(str(content)))


def is_chunk_ref(text: str) -> bool:
    """Return whether the whole string is a chunk reference."""
    return bool(text and CHUNK_REF_RE.fullmatch(str(text).strip()))


def strip_chunk_refs(content: str, *, remove_labels: bool = False) -> str:
    """Remove chunk references and optional standalone image/table labels."""
    if not content:
        return ""

    cleaned = CHUNK_REF_RE.sub("", str(content))
    if not remove_labels:
        return cleaned

    lines = [
        line for line in cleaned.splitlines()
        if not REFERENCE_LABEL_RE.fullmatch(line.strip())
    ]
    return "\n".join(lines)
