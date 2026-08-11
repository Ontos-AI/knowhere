"""Tests for CJK text normalization in shared.utils.text_utils.

Verifies that Traditional Chinese is normalized to Simplified before
jieba tokenization, so BM25 cross-matches 繁/简 variants (e.g.
'營收' ↔ '营收'). English, numbers, and punctuation must be untouched.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.utils.text_utils import (  # noqa: E402
    _normalize_cjk_to_simplified,
    tokenize_for_retrieval,
    tokenize_contents_for_retrieval,
    tokenize2stw_remove,
)


def test_normalize_traditional_to_simplified() -> None:
    assert _normalize_cjk_to_simplified("營收") == "营收"
    assert _normalize_cjk_to_simplified("電腦") == "电脑"
    assert _normalize_cjk_to_simplified("計算機") == "计算机"


def test_normalize_leaves_english_unchanged() -> None:
    assert _normalize_cjk_to_simplified("NVIDIA FY27 revenue") == "NVIDIA FY27 revenue"
    assert _normalize_cjk_to_simplified("hello world 123") == "hello world 123"


def test_normalize_leaves_punctuation_unchanged() -> None:
    assert _normalize_cjk_to_simplified("营收，同比增长。") == "营收，同比增长。"


def test_normalize_handles_empty() -> None:
    assert _normalize_cjk_to_simplified("") == ""


def test_normalize_mixed_traditional_simplified() -> None:
    # Mixed 繁/简 in the same string — both should normalize to simplified
    assert _normalize_cjk_to_simplified("營收增长") == "营收增长"


def test_tokenize_for_retrieval_normalizes_traditional() -> None:
    traditional_tokens = tokenize_for_retrieval("營收同比增長")
    simplified_tokens = tokenize_for_retrieval("营收同比增长")
    assert traditional_tokens == simplified_tokens


def test_tokenize_for_retrieval_normalizes_mixed() -> None:
    mixed_tokens = tokenize_for_retrieval("營收增长 NVIDIA")
    simplified_tokens = tokenize_for_retrieval("营收增长 NVIDIA")
    assert mixed_tokens == simplified_tokens


def test_tokenize_for_retrieval_leaves_english_unchanged() -> None:
    tokens = tokenize_for_retrieval("NVIDIA quarterly revenue")
    lowered = [t.lower() for t in tokens]
    assert "nvidia" in lowered
    assert "quarterly" in lowered


def test_tokenize_contents_for_retrieval_normalizes_traditional() -> None:
    traditional = tokenize_contents_for_retrieval(["營收同比增長"], link_char=" ")
    simplified = tokenize_contents_for_retrieval(["营收同比增长"], link_char=" ")
    assert traditional == simplified


def test_tokenize2stw_remove_normalizes_traditional() -> None:
    traditional = tokenize2stw_remove(["營收同比增長"])
    simplified = tokenize2stw_remove(["营收同比增长"])
    assert traditional == simplified


def test_traditional_and_simplified_produce_same_tokens_cross_match() -> None:
    # The core regression: a doc ingested with 繁体 should be findable
    # by a query in 简体, and vice versa.
    ingest_tokens = tokenize_contents_for_retrieval(
        ["台積電營收創歷史新高"], link_char=" "
    )
    query_tokens_traditional = tokenize_for_retrieval("台積電營收")
    query_tokens_simplified = tokenize_for_retrieval("台积电营收")

    # Both query variants should produce tokens that appear in the ingest tokens
    ingest_token_set = set(ingest_tokens[0].split()) if ingest_tokens else set()
    for query_tokens in [query_tokens_traditional, query_tokens_simplified]:
        for token in query_tokens:
            if token in ingest_token_set:
                break
        else:
            assert False, f"Query token {query_tokens} not found in ingest tokens {ingest_token_set}"
