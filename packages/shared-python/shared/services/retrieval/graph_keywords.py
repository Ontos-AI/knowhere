from __future__ import annotations

import math
import re
from typing import Any

MIN_KEYWORD_OVERLAP = 3
KEYWORD_SCORE_WEIGHT = 1.0
MIN_SCORE_THRESHOLD = 0.8


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
