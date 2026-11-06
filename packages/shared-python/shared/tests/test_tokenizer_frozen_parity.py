"""Vectors generated from git HEAD text_utils before the cooperative refactor.

Frozen source SHA256: 09589ab38f3f5af86addc858d7762588eea520b774d8c85a1ad67d0a9455b157.
The actual pre-change module was loaded from `git show HEAD:.../text_utils.py`.
"""

from collections.abc import Iterable

import pytest

from shared.utils import text_utils

_TEXT: str = "The 风险及操作风险 Alpha A/B, revenue 2.5 million 2026年 42万元. [images/a.png] image-4 TABLE_abc_TABLE\n财务模型 风险 财务模型"
_ALL: list[str] = ["the", "风险", "操作", "风险", "alpha", "a/b", "revenue", "2.5", "million", "2026", "42", "万元", "table", "abc", "table", "财务", "模型", "风险", "财务", "模型"]


@pytest.mark.parametrize("useFallback", [False, True])
@pytest.mark.parametrize("stopwords,dedupe,minimum,expected", [
    (None, False, 2, _ALL[1:]),
    ([], False, 2, _ALL),
    ([], True, 2, ["the", "风险", "操作", "alpha", "a/b", "revenue", "2.5", "million", "2026", "42", "万元", "table", "abc", "财务", "模型"]),
    (["风险", "THE", "财务"], False, 2, ["操作", "alpha", "a/b", "revenue", "2.5", "million", "2026", "42", "万元", "table", "abc", "table", "模型", "模型"]),
    ([], False, 1, _ALL),
])
def test_tokenizer_matches_frozen_prechange_vectors(useFallback: bool, stopwords: Iterable[str] | None,
    dedupe: bool, minimum: int, expected: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    if useFallback:
        monkeypatch.setattr(text_utils, "_blingfire_text_to_words", None)
    assert text_utils.tokenize_for_retrieval(_TEXT, stopwords=stopwords,
        dedupe=dedupe, min_token_length=minimum) == expected
