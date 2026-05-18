from __future__ import annotations

from typing import Any

from loguru import logger

from shared.services.text_processing.tokenization import tokenize_for_retrieval


def tokenize_query_for_ranker(query: str) -> list[str]:
    return tokenize_for_retrieval(query, dedupe=True)


def rank_rows_by_bm25(
    rows: list[dict[str, Any]],
    query_tokens: list[str],
    *,
    search_field: str,
) -> list[dict[str, Any]]:
    """Rank matching rows with BM25 over pre-tokenized search text."""
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        return _rank_rows_by_token_overlap(
            rows,
            query_tokens,
            search_field=search_field,
        )

    corpus: list[list[str]] = []
    ranked_rows: list[dict[str, Any]] = []
    query_token_set = set(query_tokens)
    for row in rows:
        tokens = _get_search_tokens(row, search_field=search_field)
        if not tokens or not query_token_set.intersection(tokens):
            continue
        corpus.append(tokens)
        ranked_rows.append(row)

    if not corpus or not query_tokens:
        return []

    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(query_tokens)

    for index, row in enumerate(ranked_rows):
        row["score"] = float(scores[index])

    ranked_rows.sort(key=lambda row: row["score"], reverse=True)
    return ranked_rows


def _rank_rows_by_token_overlap(
    rows: list[dict[str, Any]],
    query_tokens: list[str],
    *,
    search_field: str,
) -> list[dict[str, Any]]:
    logger.warning("rank_bm25 not installed, skipping BM25 re-rank")
    ranked_rows: list[dict[str, Any]] = []
    query_token_set = set(query_tokens)
    for row in rows:
        tokens = _get_search_tokens(row, search_field=search_field)
        overlap = len(query_token_set.intersection(tokens))
        if overlap <= 0:
            continue
        row["score"] = float(overlap)
        ranked_rows.append(row)
    ranked_rows.sort(key=lambda row: row["score"], reverse=True)
    return ranked_rows


def _get_search_tokens(row: dict[str, Any], *, search_field: str) -> list[str]:
    return [token for token in str(row.get(search_field) or "").split() if token]
