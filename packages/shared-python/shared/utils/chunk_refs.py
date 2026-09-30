import re
from typing import List, TypedDict

RESOURCE_PATH_REF_PATTERN = (
    r"\[(?:images/[^\n]+?\.(?:png|jpe?g|gif|webp)|tables/[^\n]+?\.(?:html|csv))\]"
)
CHUNK_REF_PATTERN = RESOURCE_PATH_REF_PATTERN
REFERENCE_LABEL_PATTERN = r"^\s*(?:image|table)-\d+\s*$"

RESOURCE_PATH_REF_RE = re.compile(RESOURCE_PATH_REF_PATTERN, re.IGNORECASE)
CHUNK_REF_RE = re.compile(CHUNK_REF_PATTERN, re.IGNORECASE)
REFERENCE_LABEL_RE = re.compile(REFERENCE_LABEL_PATTERN, re.IGNORECASE)


class ChunkRefSpan(TypedDict):
    ref: str
    start: int
    end: int


def build_chunk_ref(resource_path: str) -> str:
    """Format a chunk reference as a readable path token."""
    normalized_path = str(resource_path or "").strip()
    return f"[{normalized_path}]" if normalized_path else ""


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


def match_resource_path_ref(text: str, ref: str) -> re.Match[str] | None:
    """Find the complete ``[images|tables/...ext]`` span for a stored ref.

    Stored ``connect_to.ref`` may stop at an inner ``]``. A complete
    placeholder still ends with an image or table extension plus ``]``.
    """
    haystack = str(text or "")
    raw = str(ref or "").strip()
    if not raw or not haystack:
        return None
    prefixes = [raw]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if inner:
            prefixes.append(inner)
    else:
        prefixes.append(f"[{raw}]")
    exact: re.Match[str] | None = None
    prefixed: re.Match[str] | None = None
    for match in RESOURCE_PATH_REF_RE.finditer(haystack):
        found = match.group(0)
        if found == raw or found[1:-1] == raw or found == f"[{raw}]":
            if exact is None or match.start() < exact.start():
                exact = match
            continue
        if any(found.startswith(prefix) for prefix in prefixes if prefix.startswith("[")):
            if prefixed is None or match.start() < prefixed.start():
                prefixed = match
    return exact or prefixed


def extract_chunk_ref_spans(content: str) -> List[ChunkRefSpan]:
    """Extract chunk references with zero-based character offsets."""
    if not content:
        return []

    return [
        {
            "ref": match.group(0),
            "start": match.start(),
            "end": match.end(),
        }
        for match in CHUNK_REF_RE.finditer(str(content))
    ]


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
        line
        for line in cleaned.splitlines()
        if not REFERENCE_LABEL_RE.fullmatch(line.strip())
    ]
    return "\n".join(lines)
