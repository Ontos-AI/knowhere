"""Unit tests for the term-channel scoring helper.

The previous 100.0 boost for `query_lower in haystack` was unreachable
for CJK content because `term_search_text` is jieba-segmented. The
helper now uses a smooth log-scale that works for every language.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "packages" / "shared-python"))

channels = importlib.import_module("shared.services.retrieval.search.channels")
_score_term_match = channels._score_term_match


def test_no_tokens_returns_zero():
    assert _score_term_match("foo bar", [], "foo bar baz") == 0.0


def test_no_match_returns_zero():
    assert _score_term_match("foo bar", ["foo", "bar"], "baz qux") == 0.0


def test_single_match_scores_above_one():
    score = _score_term_match("施工", ["施工"], "施工现场 安全 规范")
    assert 1.0 < score < 2.0


def test_more_hits_score_higher():
    one = _score_term_match("alpha beta", ["alpha", "beta"], "alpha delta")
    two = _score_term_match("alpha beta", ["alpha", "beta"], "alpha beta delta")
    three = _score_term_match("alpha beta", ["alpha", "beta", "gamma"], "alpha beta gamma")
    assert one < two
    assert two < three


def test_cjk_multi_token_score_is_well_defined():
    """Regression for the unreachable 100.0 branch.

    All three CJK tokens appear in the haystack (separated by jieba
    spaces in production). Score must be above 1.0 and bounded.
    """
    score = _score_term_match("施工 安全 规范", ["施工", "安全", "规范"], "施工 安全 规范 守则")
    assert score > 1.0
    assert score < 5.0


def test_score_matches_within_query_tokens():
    a = _score_term_match("foo", ["foo"], "foo bar")
    b = _score_term_match("foo", ["FOO"], "foo bar")
    # The function expects already-lowered tokens (callers pass
    # tokenize_query_for_ranker output, which is lowered).
    assert a > 0
    # Tokens passed in raw case may not match — that is the caller's
    # responsibility, not the helper's.
    assert b == 0
