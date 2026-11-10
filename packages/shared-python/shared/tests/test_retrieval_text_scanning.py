"""Legacy marker semantics and cooperative scanning of large retrieval text."""

from itertools import groupby
import os
import random
import re

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-text-scan-unit")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.retrieval.publication_preparation_cache import PublicationPreparationCache
from shared.utils import text_utils
from shared.utils.chunk_refs import CHUNK_REF_PATTERN

_BASELINE_MARKERS: re.Pattern[str] = re.compile(
    rf"{CHUNK_REF_PATTERN}|image-\d+|table-\d+", re.IGNORECASE
)


def _baselineSpans(text: str) -> list[tuple[str, bool]]:
    cleaned: str = _BASELINE_MARKERS.sub("", text)
    return [("".join(chars), isCjk) for isCjk, chars in groupby(cleaned, lambda char: "\u4e00" <= char <= "\u9fff")]


@pytest.mark.parametrize("text", ["evidence " * 4096, "[images/" * 2048], ids=["plain", "malformed-markers"])
def test_large_scan_checkpoints_before_calling_opaque_english_tokenizer(text: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    def tokenize(segment: str) -> list[str]:
        calls.append(segment)
        return ["evidence"]
    monkeypatch.setattr(text_utils, "_tokenize_english_segment", tokenize)
    steps = text_utils._iterate_retrieval_token_steps(text, stopwords=[])
    for _ in range(4):
        assert next(steps) is None
        assert calls == []
    assert [step for step in steps if step is not None] == ["evidence"]
    assert calls == [text]


def test_cached_span_validation_checkpoints_before_long_span(monkeypatch: pytest.MonkeyPatch) -> None:
    text: str = "财" * 16384
    monkeypatch.setattr(text_utils, "_tokenize_cjk_segment", lambda segment: ["财务"])
    validated: list[str] = []
    with PublicationPreparationCache.scope(source_id="cooperative-scan", content_version="1", index_format_version=2):
        assert PublicationPreparationCache.tokenize(text) == ["财务"]
        steps = PublicationPreparationCache.iterateTokenSteps(text, validateCjkSpan=validated.append)
        for _ in range(3):
            assert next(steps) is None
            assert validated == []
        assert [step for step in steps if step is not None] == ["财务"]
    assert validated == [text]


def test_marker_removal_and_language_spans_match_baseline() -> None:
    fragments: list[str] = [
        "甲[images/outer [tables/inner.csv] suffix.webp]乙",
        "甲[images/outer [tables/inner.csv]乙",
        "甲[tables/outer [images/inner.PNG] suffix.CSV]乙",
        "甲[images/.png] [tables/.csv]乙",
        "甲[images/.png] first.GIF] trailing.jpg]乙",
        "甲[images/a.png\n[tables/b.html]乙",
        "甲[images/a\r\u2028b.JPEG]乙",
        "甲[İMAGEſ/a.GİF]IMAGE-١٢３TABLE-𝟏𝟐乙",
        "[images/[images/.png] image-99 a.webp] [tables/one.html]",
        "non-CJK文字[images/a.png]文字 evidence-image-１２word",
        "x" * 4092 + "[tables/nested [x].csv]财务",
        "[images/" + "x" * 4086 + ".JPEG]财务",
        "x" * 4090 + "IMAGE-" + "١" * 8200 + "财务",
    ]
    randomSource = random.Random(812)
    pieces: list[str] = ["甲", "word", "\n", "[images/", "[tables/", ".png]", ".html]", ".JPG]", "image-٣", "table-²", "]", " ", ".csv]", "ImaGe-１２٣"]
    fragments.extend("".join(randomSource.choices(pieces, k=40)) for _ in range(300))
    for text in fragments:
        assert [step for step in text_utils._iterate_retrieval_segment_steps(text) if step is not None] == _baselineSpans(text)
