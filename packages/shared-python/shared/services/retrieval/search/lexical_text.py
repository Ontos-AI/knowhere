"""
Lexical text builders for canonical retrieval publication.
"""

from __future__ import annotations

from typing import Any, Optional

from shared.services.chunks.document_path import split_document_path
from shared.utils.text_utils import tokenize_contents_for_retrieval


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
    if _is_table_chunk(chunk):
        content = _table_search_source_text(chunk)
        if not content:
            return None
        return build_lexical_text(content)

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


def section_path_from_chunk_path(
    source_path: Optional[str],
    *,
    source_file_name: str | None = None,
) -> str:
    """Extract section hierarchy from chunk path.

    Expected format: "<file>.ext/<Section>/<Subsection>/..."
    Returns " / "-joined section parts, or "Root" if no section hierarchy.
    """
    if not source_path:
        return "Root"
    _, section_parts = split_document_path(
        source_path,
        source_file_name=source_file_name,
    )
    if not section_parts:
        return "Root"
    return " / ".join(section_parts)


def build_path_lexical_text(
    source_path: Optional[str],
    *,
    source_file_name: str | None = None,
) -> Optional[str]:
    section_path = section_path_from_chunk_path(
        source_path,
        source_file_name=source_file_name,
    )
    if not section_path:
        return None
    normalized_path = section_path.replace(" / ", " ")
    return build_lexical_text(normalized_path)


def build_content_search_text(
    chunk: dict[str, Any],
    *,
    section_summary: Optional[str] = None,
) -> Optional[str]:
    """Pre-tokenized content field for retrieval BM25 scoring.

    Includes the chunk's own node-level ``summary`` and flattened ``entities``
    (§4.7) so the richer VLM/entity output is lexically searchable at build time —
    not just the late-backfilled section rollup. This closes the long-standing
    "VLM summary 未入 content_search_text" gap.
    """
    if _is_table_chunk(chunk):
        content = _table_search_source_text(chunk)
    else:
        content = str(chunk.get("content") or chunk.get("text") or "").strip()
    chunk_summary = _chunk_summary_text(chunk)
    entity_text = _chunk_entity_text(chunk)
    if not content and not chunk_summary and not entity_text:
        return None
    parts = [part for part in [content, chunk_summary, entity_text] if part]
    if section_summary and str(section_summary).strip():
        parts.append(str(section_summary).strip())
    raw = " ".join(parts)
    tokens = tokenize_contents_for_retrieval([raw], stopwords=[], link_char=" ")
    return tokens[0] if tokens else raw


def _chunk_summary_text(chunk: dict[str, Any]) -> str:
    metadata = chunk.get("metadata") or chunk.get("chunk_metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return str(metadata.get("summary") or chunk.get("summary") or "").strip()


def _chunk_entity_text(chunk: dict[str, Any]) -> str:
    """Flatten typed entities (§4.4) into a space-joined surface-form string."""
    metadata = chunk.get("metadata") or chunk.get("chunk_metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    entities = metadata.get("entities")
    if not isinstance(entities, list):
        return ""
    texts = [
        str(item.get("text", "")).strip()
        for item in entities
        if isinstance(item, dict) and str(item.get("text", "")).strip()
    ]
    return " ".join(texts)


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
    if _is_table_chunk(chunk):
        content = _table_search_source_text(chunk)
    else:
        content = str(chunk.get("content") or chunk.get("text") or "").strip()
    path = str(path_text or "").strip()
    combined = f"{content} {path}".strip()
    return combined if combined else None


def _is_table_chunk(chunk: dict[str, Any]) -> bool:
    raw_type = chunk.get("type") or chunk.get("chunk_type") or ""
    return str(raw_type).strip().split("\n", 1)[0].lower() == "table"


def _table_search_source_text(chunk: dict[str, Any]) -> str:
    metadata = chunk.get("metadata") or chunk.get("chunk_metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    keyword_value = metadata.get("keywords") or chunk.get("keywords") or []
    if isinstance(keyword_value, list):
        keyword_text = " ".join(
            str(keyword).strip()
            for keyword in keyword_value
            if str(keyword).strip()
        )
    else:
        keyword_text = str(keyword_value or "").replace(";", " ").strip()

    parts = [
        str(metadata.get("summary") or chunk.get("summary") or "").strip(),
        keyword_text,
        str(metadata.get("caption") or chunk.get("caption") or "").strip(),
    ]
    return " ".join(part for part in parts if part)
