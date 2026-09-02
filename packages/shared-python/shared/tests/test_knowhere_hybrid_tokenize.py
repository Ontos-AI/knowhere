"""Word-level map-unit tokenization (replaces character-level CJK regex)."""

from __future__ import annotations

from shared.services.retrieval.nav.knowhere_hybrid import (
    MAP_UNIT_INDEX_FORMAT_VERSION,
    build_content_search_text,
    build_path_search_text,
    tokenize_for_retrieval,
    tokenize_query_for_ranker,
)
from shared.utils.text_utils import tokenize_for_retrieval as tokenize_publication


def test_map_unit_index_format_is_word_level_v2() -> None:
    assert MAP_UNIT_INDEX_FORMAT_VERSION == 2


def test_query_uses_chinese_word_tokens_not_characters() -> None:
    tokens = tokenize_query_for_ranker("冠心病的诊断标准")
    assert "冠心病" in tokens
    assert "诊断" in tokens
    assert "标准" in tokens
    assert "冠" not in tokens
    assert "诊" not in tokens


def test_index_text_matches_publication_tokenizer() -> None:
    text = "冠心病患者的诊断标准与心肌炎鉴别"
    map_tokens = tokenize_for_retrieval(text, dedupe=False)
    pub_tokens = tokenize_publication(
        text, stopwords=[], dedupe=False, min_token_length=2
    )
    assert map_tokens == pub_tokens
    assert "冠心病" in map_tokens
    assert "心肌炎" in map_tokens


def test_build_search_text_joins_word_tokens() -> None:
    content = build_content_search_text("冠心病诊断标准")
    path = build_path_search_text(section_path="指南 / 冠心病 / 诊断")
    assert "冠心病" in content.split()
    assert "诊断" in content.split()
    assert "冠心病" in path.split()
    # Single CJK characters must not appear as standalone tokens.
    assert "冠" not in content.split()
    assert "冠" not in path.split()
