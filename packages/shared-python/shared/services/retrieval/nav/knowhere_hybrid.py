"""KnowWhere-style map-unit BM25 (path + content).

Query-time scoring uses persisted corpus statistics. In-memory row/stream
scorers were retired; the formula itself lives in ``_score_streaming_units``.

Reference: https://github.com/Ontos-AI/knowhere
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from shared.utils.text_utils import tokenize_for_retrieval as _tokenize_word_level

RRF_K = 60
CHANNEL_WEIGHT_PATH = 1.0
CHANNEL_WEIGHT_CONTENT = 2.0
# Persisted map-unit token format. Bump when tokenizer semantics change.
# v1: character-level CJK regex. v2: word-level jieba (text_utils).
MAP_UNIT_INDEX_FORMAT_VERSION = 2


def tokenize_for_retrieval(text: str, *, dedupe: bool = True) -> List[str]:
    """Word-level retrieval tokens (jieba + English), aligned with chunk publication.

    Uses the same knobs as ``search.lexical_text``: no stopword filtering and
    ``min_token_length=2`` so map-unit indexes match ``document_chunks`` search
    text. Character-level regex tokenization was removed.
    """
    return _tokenize_word_level(
        text,
        stopwords=[],
        dedupe=dedupe,
        min_token_length=2,
    )


def tokenize_query_for_ranker(query: str) -> List[str]:
    return tokenize_for_retrieval(query, dedupe=True)


def _space_join_tokens(text: str) -> str:
    return " ".join(tokenize_for_retrieval(text, dedupe=False))


def build_content_search_text(
    content: str, *, section_summary: Optional[str] = None
) -> str:
    parts = [str(content or "").strip()]
    if section_summary and str(section_summary).strip():
        parts.append(str(section_summary).strip())
    raw = " ".join(p for p in parts if p)
    return _space_join_tokens(raw) if raw else ""


def build_path_search_text(
    *,
    source_file_name: Optional[str] = None,
    section_path: Optional[str] = None,
    section_title: Optional[str] = None,
) -> str:
    parts = [
        str(v).strip()
        for v in (source_file_name, section_path, section_title)
        if v and str(v).strip()
    ]
    if not parts:
        return ""
    return _space_join_tokens(" ".join(parts))


def build_term_search_text(content: str, *, path_text: Optional[str] = None) -> str:
    combined = f"{str(content or '').strip()} {str(path_text or '').strip()}".strip()
    return combined


def map_channel_weights() -> Tuple[float, float]:
    """Path and content weights for persisted map scoring."""
    path_w = float(
        os.environ.get(
            "NAV_MAP_CHANNEL_WEIGHT_PATH",
            os.environ.get(
                "NAV_DISCOVERY_CHANNEL_WEIGHT_PATH", str(CHANNEL_WEIGHT_PATH)
            ),
        ).strip()
        or CHANNEL_WEIGHT_PATH
    )
    content_w = float(
        os.environ.get(
            "NAV_MAP_CHANNEL_WEIGHT_CONTENT",
            os.environ.get(
                "NAV_DISCOVERY_CHANNEL_WEIGHT_CONTENT", str(CHANNEL_WEIGHT_CONTENT)
            ),
        ).strip()
        or CHANNEL_WEIGHT_CONTENT
    )
    return path_w, content_w


def _score_streaming_units(
    units: Sequence["_StreamingManyUnit"],
    *,
    path_stats: "_StreamingBm25Stats",
    content_stats: "_StreamingBm25Stats",
    query_tokens: List[str],
) -> Dict[str, float]:
    path_by_id: Dict[str, float] = {}
    content_by_id: Dict[str, float] = {}
    unit_ids = list(dict.fromkeys(unit.unit_id for unit in units))
    for unit in units:
        path_score = path_stats.score(
            unit.path_length, unit.path_frequencies, query_tokens
        )
        content_score = content_stats.score(
            unit.content_length, unit.content_frequencies, query_tokens
        )
        path_by_id[unit.unit_id] = path_score
        content_by_id[unit.unit_id] = content_score

    path_rows = [
        (score, unit_id) for unit_id, score in path_by_id.items() if score > 0.0
    ]
    content_rows = [
        (score, unit_id) for unit_id, score in content_by_id.items() if score > 0.0
    ]
    path_rows.sort(key=lambda item: (-item[0], item[1]))
    content_rows.sort(key=lambda item: (-item[0], item[1]))
    path_weight, content_weight = map_channel_weights()
    rrf_k = int(
        os.environ.get(
            "NAV_MAP_RRF_K",
            os.environ.get("NAV_DISCOVERY_RRF_K", str(RRF_K)),
        ).strip()
        or RRF_K
    )
    fused: Dict[str, float] = {unit_id: 0.0 for unit_id in unit_ids}
    for rank, (_score, unit_id) in enumerate(path_rows):
        fused[unit_id] = fused.get(unit_id, 0.0) + path_weight / (rrf_k + rank + 1)
    for rank, (_score, unit_id) in enumerate(content_rows):
        fused[unit_id] = fused.get(unit_id, 0.0) + content_weight / (rrf_k + rank + 1)
    return {unit_id: round(score, 6) for unit_id, score in fused.items()}


@dataclass(frozen=True)
class _StreamingManyUnit:
    unit_id: str
    path_length: int
    content_length: int
    path_frequencies: Mapping[str, int]
    content_frequencies: Mapping[str, int]


@dataclass(frozen=True)
class PersistedBm25Stats:
    """Corpus statistics needed to reproduce ``BM25Okapi`` exactly."""

    document_count: int
    total_length: int
    document_frequency: Mapping[str, int]
    average_idf: float


@dataclass(frozen=True)
class PersistedScoreUnit:
    """Query-specific frequencies for one persisted map unit."""

    unit_id: str
    path_length: int
    content_length: int
    path_frequencies: Mapping[str, int]
    content_frequencies: Mapping[str, int]


@dataclass(frozen=True)
class PersistedScoreCorpus:
    """Compact query projection loaded from the map-unit index."""

    units: Sequence[PersistedScoreUnit]
    path_stats: PersistedBm25Stats
    content_stats: PersistedBm25Stats


def score_persisted_corpus_many(
    corpus: PersistedScoreCorpus,
    queries: Sequence[str],
) -> Dict[str, Dict[str, float]]:
    """Apply the existing BM25/RRF scorer to persisted query projections."""
    unique_queries = list(dict.fromkeys(str(query) for query in queries))
    if not unique_queries:
        return {}
    path_stats = _restore_bm25_stats(corpus.path_stats)
    content_stats = _restore_bm25_stats(corpus.content_stats)
    units = [
        _StreamingManyUnit(
            unit_id=unit.unit_id,
            path_length=unit.path_length,
            content_length=unit.content_length,
            path_frequencies=unit.path_frequencies,
            content_frequencies=unit.content_frequencies,
        )
        for unit in corpus.units
    ]
    return {
        query: _score_streaming_units(
            units,
            path_stats=path_stats,
            content_stats=content_stats,
            query_tokens=tokenize_query_for_ranker(query),
        )
        for query in unique_queries
    }


def _restore_bm25_stats(source: PersistedBm25Stats) -> "_StreamingBm25Stats":
    stats = _StreamingBm25Stats.empty()
    stats.document_count = source.document_count
    stats.total_length = source.total_length
    stats.average_length = (
        source.total_length / source.document_count if source.document_count else 0.0
    )
    idf_by_token: Dict[str, float] = {}
    for token, frequency in source.document_frequency.items():
        idf = math.log(source.document_count - frequency + 0.5) - math.log(
            frequency + 0.5
        )
        idf_by_token[token] = 0.25 * source.average_idf if idf < 0.0 else idf
    stats.idf_by_token = idf_by_token
    return stats


class _StreamingBm25Stats:
    """Exact BM25Okapi corpus statistics collected without row retention."""

    def __init__(self) -> None:
        self.document_count: int = 0
        self.total_length: int = 0
        self.average_length: float = 0.0
        self.idf_by_token: Dict[str, float] = {}

    @classmethod
    def empty(cls) -> "_StreamingBm25Stats":
        return cls()

    def score(
        self,
        document_length: int,
        frequencies: Dict[str, int],
        query_tokens: List[str],
    ) -> float:
        if (
            not frequencies
            or not query_tokens
            or not self.document_count
            or self.average_length <= 0.0
        ):
            return 0.0
        denominator_base = 1.5 * (
            1.0 - 0.75 + 0.75 * document_length / self.average_length
        )
        score = 0.0
        for token in query_tokens:
            frequency = frequencies.get(token, 0)
            if not frequency:
                continue
            idf = self.idf_by_token.get(token, 0.0)
            score += idf * (frequency * 2.5 / (frequency + denominator_base))
        return score
