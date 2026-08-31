"""Shared helpers for building persisted BM25 corpora from DB rows."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from shared.services.retrieval.nav.knowhere_hybrid import PersistedBm25Stats


def average_idf_from_unit_dfs(
    *,
    unit_count: int,
    token_document_frequency: Mapping[str, int],
) -> float:
    """rank_bm25 Okapi average IDF over every token that appears in the corpus."""
    if unit_count <= 0 or not token_document_frequency:
        return 0.0
    idfs = [
        math.log(unit_count - frequency + 0.5) - math.log(frequency + 0.5)
        for frequency in token_document_frequency.values()
        if frequency > 0
    ]
    if not idfs:
        return 0.0
    return sum(idfs) / len(idfs)


def combine_average_idf(parts: Sequence[tuple[float, int]]) -> float:
    """Unit-count-weighted mean of per-revision average IDF values."""
    total_units = sum(int(unit_count) for _average, unit_count in parts)
    if total_units <= 0:
        return 0.0
    return (
        sum(float(average) * int(unit_count) for average, unit_count in parts)
        / total_units
    )


def build_channel_bm25_stats(
    *,
    unit_rows: Sequence[Mapping[str, Any]],
    map_unit_id_field: str,
    length_field: str,
    channel: str,
    query_tokens: Sequence[str],
    frequencies: Mapping[tuple[str, str], Mapping[str, int]],
    average_idf: float,
) -> PersistedBm25Stats:
    """Build channel stats from already-fetched unit rows and query-token freqs."""
    lengths = [
        int(row[length_field]) for row in unit_rows if int(row[length_field]) > 0
    ]
    document_count = len(lengths)
    document_frequency = {
        token: sum(
            1
            for row in unit_rows
            if frequencies.get((str(row[map_unit_id_field]), channel), {}).get(
                token, 0
            )
            > 0
        )
        for token in query_tokens
    }
    return PersistedBm25Stats(
        document_count=document_count,
        total_length=sum(lengths),
        document_frequency=document_frequency,
        average_idf=float(average_idf),
    )
