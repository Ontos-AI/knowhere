"""
Lexical text builders for canonical retrieval publication.
"""

from __future__ import annotations

from typing import Any, Optional

from shared.services.text_processing.tokenization import tokenize_contents_for_retrieval


def normalize_section_path(path: Optional[str]) -> str:
    """Return the canonical section path representation used by retrieval."""
    raw = str(path or "").strip()
    if not raw:
        return "Root"
    parts = split_section_path(raw)
    if not parts:
        return "Root"
    return " / ".join(parts)


def split_section_path(path: Optional[str]) -> list[str]:
    """Split either canonical ``" / "`` paths or raw slash-separated paths."""
    raw = str(path or "").strip()
    if not raw or raw == "Root":
        return []
    if " / " in raw:
        return [p.strip() for p in raw.split(" / ") if p.strip()]
    return [p.strip() for p in raw.split("/") if p.strip()]


def build_lexical_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    tokens = tokenize_contents_for_retrieval([text], stopwords=[], link_char=" ", dedupe=True)
    token_text = tokens[0] if tokens else ""
    lexical_parts = [part for part in [text, token_text] if part]
    return "\n".join(lexical_parts) if lexical_parts else text


def build_content_lexical_text(chunk: dict[str, Any]) -> Optional[str]:
    content = str(chunk.get("content") or chunk.get("text") or "").strip()
    if not content:
        return None
    metadata = chunk.get("metadata") or {}
    tokens = metadata.get("tokens") if isinstance(metadata, dict) else None
    if isinstance(tokens, list):
        token_text = " ".join(
            str(token).strip() for token in tokens if str(token).strip()
        )
    else:
        token_text = ""
    lexical_parts = [part for part in [content, token_text] if part]
    return "\n".join(lexical_parts) if lexical_parts else content


def section_path_from_chunk_path(source_path: Optional[str]) -> str:
    """Extract section hierarchy from chunk path.

    Expected format: "<kb_root>/<file>.ext/<Section>/<Subsection>/..."
    Returns " / "-joined section parts, or "Root" if no section hierarchy.
    """
    if not source_path:
        return "Root"
    parts = split_section_path(source_path)
    section_parts = parts[2:]  # skip kb_root + filename
    if not section_parts:
        return "Root"
    return normalize_section_path(" / ".join(section_parts))


def build_path_lexical_text(source_path: Optional[str]) -> Optional[str]:
    section_path = section_path_from_chunk_path(source_path)
    if not section_path:
        return None
    normalized_path = section_path.replace(" / ", " ")
    return build_lexical_text(normalized_path)


def build_content_search_text(
    chunk: dict[str, Any],
    *,
    section_summary: Optional[str] = None,
) -> Optional[str]:
    """Pre-tokenized content field for retrieval BM25 scoring."""
    content = str(chunk.get("content") or chunk.get("text") or "").strip()
    if not content:
        return None
    parts = [content]
    if section_summary and str(section_summary).strip():
        parts.append(str(section_summary).strip())
    raw = " ".join(parts)
    tokens = tokenize_contents_for_retrieval([raw], stopwords=[], link_char=" ")
    return tokens[0] if tokens else raw


def build_path_search_text(
    *,
    source_file_name: Optional[str],
    section_path: Optional[str],
    section_title: Optional[str],
    section_summary: Optional[str],
) -> Optional[str]:
    """Pre-tokenized path field for retrieval BM25 scoring."""
    parts = [
        str(v).strip()
        for v in [source_file_name, section_path, section_title, section_summary]
        if v and str(v).strip()
    ]
    if not parts:
        return None
    raw = " ".join(parts)
    tokens = tokenize_contents_for_retrieval([raw], stopwords=[], link_char=" ")
    return tokens[0] if tokens else raw


def build_term_search_text(
    chunk: dict[str, Any],
    *,
    path_text: Optional[str] = None,
) -> Optional[str]:
    """Raw combined field for grep channel: content + path (not tokenized)."""
    content = str(chunk.get("content") or chunk.get("text") or "").strip()
    path = str(path_text or "").strip()
    combined = f"{content} {path}".strip()
    return combined if combined else None
