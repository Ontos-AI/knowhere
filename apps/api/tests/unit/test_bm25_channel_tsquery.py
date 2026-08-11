"""Unit tests for BM25 channel Postgres FTS prefilter token preparation."""

from __future__ import annotations

from shared.services.retrieval.search.channels import (
    _MAX_FTS_QUERY_TOKENS,
    _prepare_fts_tokens,
)


def test_returns_empty_for_no_tokens() -> None:
    assert _prepare_fts_tokens([]) == []


def test_keeps_token_order() -> None:
    assert _prepare_fts_tokens(["alpha", "beta"]) == ["alpha", "beta"]


def test_strips_surrounding_whitespace() -> None:
    assert _prepare_fts_tokens(["  alpha  ", "beta"]) == ["alpha", "beta"]


def test_drops_blank_tokens() -> None:
    assert _prepare_fts_tokens(["", "   ", "alpha"]) == ["alpha"]


def test_returns_empty_when_every_token_is_blank() -> None:
    assert _prepare_fts_tokens(["", "   "]) == []


def test_caps_token_count() -> None:
    tokens = [f"tok{index}" for index in range(_MAX_FTS_QUERY_TOKENS + 25)]
    assert len(_prepare_fts_tokens(tokens)) == _MAX_FTS_QUERY_TOKENS


def test_preserves_cjk_tokens() -> None:
    assert _prepare_fts_tokens(["合同", "条款"]) == ["合同", "条款"]


def test_passes_tsquery_operators_through_untouched() -> None:
    # Tokens travel to Postgres as a text[] parameter and are lexed there, so
    # operator characters are data rather than syntax. Nothing is escaped or
    # dropped here.
    raw = ["alpha' & 'zzzz", "!beta", "a|b"]
    assert _prepare_fts_tokens(raw) == raw
