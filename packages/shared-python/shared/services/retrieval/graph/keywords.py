from __future__ import annotations

import math
import re
from typing import Any

MIN_KEYWORD_OVERLAP = 3
KEYWORD_SCORE_WEIGHT = 1.0
MIN_SCORE_THRESHOLD = 0.8
# Typed entities are higher-precision than free-form keywords, so a smaller
# overlap is meaningful for cross-document links (§4.4).
MIN_ENTITY_OVERLAP = 2


def normalize_keyword(keyword: str) -> str:
    """Normalize a keyword: lowercase, strip, collapse spaces."""
    keyword = keyword.lower().strip()
    return re.sub(r'\s+', ' ', keyword)


def extract_keywords_from_chunk_metadata(meta: dict) -> list[str]:
    """Extract keywords from chunk metadata."""
    if not isinstance(meta, dict):
        return []

    keywords = meta.get('keywords', [])
    if isinstance(keywords, list) and keywords:
        return [str(keyword) for keyword in keywords if keyword]

    tokens = meta.get('tokens', [])
    if isinstance(tokens, list) and tokens:
        return [str(token) for token in tokens if token and len(str(token)) > 1]

    return []


def normalize_entity(text: str, entity_type: str) -> tuple[str, str] | None:
    """Normalize a typed entity into a ``(type, text)`` key, or None if empty.

    Type is lower-cased; text is lower-cased and whitespace-collapsed. Typing the
    key means "Apple" the organization does not collide with "apple" the product
    once types are populated, while untyped entities still match on text alone.
    """
    norm_text = re.sub(r'\s+', ' ', str(text).lower().strip())
    if not norm_text or len(norm_text) <= 1:
        return None
    if re.match(r'^\d+[.,%]*$', norm_text):
        return None
    norm_type = re.sub(r'\s+', ' ', str(entity_type or '').lower().strip())
    return (norm_type, norm_text)


def extract_entities_from_chunk_metadata(meta: dict) -> list[tuple[str, str]]:
    """Extract normalized typed-entity keys from one chunk's metadata (§4.4)."""
    if not isinstance(meta, dict):
        return []
    entities = meta.get('entities')
    if not isinstance(entities, list):
        return []
    result: list[tuple[str, str]] = []
    for item in entities:
        if not isinstance(item, dict):
            continue
        key = normalize_entity(item.get('text', ''), item.get('type', ''))
        if key is not None:
            result.append(key)
    return result


def get_normalized_entity_set(
    chunk_metadata_list: list[dict[str, Any]],
) -> set[tuple[str, str]]:
    """Collect all normalized typed entities across a document's chunks."""
    result: set[tuple[str, str]] = set()
    for meta in chunk_metadata_list:
        result.update(extract_entities_from_chunk_metadata(meta))
    return result


def compute_entity_score(
    shared_entities: set[tuple[str, str]],
    entities_a: set[tuple[str, str]],
    entities_b: set[tuple[str, str]],
    weight: float = 1.0,
) -> float:
    """Character-length-weighted typed-entity overlap score.

    Mirrors ``compute_keyword_score`` but over ``(type, text)`` keys, weighting by
    the length of the entity text so longer, more specific entities count more.
    """
    def _weight(entities: set[tuple[str, str]]) -> int:
        return sum(len(text) for _type, text in entities)

    denominator = min(_weight(entities_a), _weight(entities_b))
    if denominator == 0:
        return 0.0
    return weight * _weight(shared_entities) / denominator


def compute_tfidf_keywords(
    chunk_metadata_list: list[dict[str, Any]],
    top_k: int = 10,
) -> list[str]:
    """Compute TF-IDF keywords from chunk metadata."""
    df_count: dict[str, int] = {}
    tf_count: dict[str, int] = {}
    total = len(chunk_metadata_list) or 1
    for meta in chunk_metadata_list:
        keywords = extract_keywords_from_chunk_metadata(meta)
        seen: set[str] = set()
        for keyword in keywords:
            if len(str(keyword)) <= 1 or re.match(r'^\d+[.,%]*$', str(keyword)):
                continue
            normalized = normalize_keyword(str(keyword))
            if not normalized:
                continue
            tf_count[normalized] = tf_count.get(normalized, 0) + 1
            if normalized not in seen:
                df_count[normalized] = df_count.get(normalized, 0) + 1
                seen.add(normalized)
    scored = [
        (term, freq * (math.log(total / (df_count.get(term, 1))) + 1))
        for term, freq in tf_count.items()
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    return [term for term, _ in scored[:top_k]]


def compute_keyword_score(
    shared_keywords: set[str],
    keywords_a: set[str],
    keywords_b: set[str],
    weight: float = 1.0,
) -> float:
    """Character-length-weighted keyword overlap score."""
    weighted_a = sum(len(keyword) for keyword in keywords_a)
    weighted_b = sum(len(keyword) for keyword in keywords_b)
    denominator = min(weighted_a, weighted_b)
    if denominator == 0:
        return 0.0
    weighted_shared = sum(len(keyword) for keyword in shared_keywords)
    return weight * weighted_shared / denominator


def get_normalized_keyword_set(chunk_metadata_list: list[dict[str, Any]]) -> set[str]:
    """Collect all normalized keywords from chunk metadata for a document."""
    result: set[str] = set()
    for meta in chunk_metadata_list:
        for keyword in extract_keywords_from_chunk_metadata(meta):
            normalized = normalize_keyword(str(keyword))
            if normalized and len(normalized) > 1 and not re.match(
                r'^\d+[.,%]*$', normalized
            ):
                result.add(normalized)
    return result


def extract_document_top_summary(chunk_metadata_list: list[dict[str, Any]]) -> str:
    """Read the parser-injected top summary from chunk metadata."""
    for meta in chunk_metadata_list:
        if not isinstance(meta, dict):
            continue
        summary = str(meta.get('document_top_summary') or '').strip()
        if summary:
            return summary
    return ''
