from __future__ import annotations

import re

from shared.utils.text_utils import _CN_EN_NUM_RE

_CN_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
EN_START_LIMIT = 15
CN_RATIO_THRESHOLD = 0.3


def normalize_md(text: str) -> str:
    """Normalize markdown string for comparison."""
    text = re.sub(r"^\s*#+\s*", "", text)
    text = re.sub(r"\s+", "", text)
    return text.lower()


def truncate_text(text: str, start_limit: int, end_limit: int) -> str:
    """Truncate text by raw character count, keeping start and end parts."""
    text = str(text)
    total_limit = start_limit + end_limit
    if len(text) <= total_limit:
        return text
    start_part = text[:start_limit]
    end_part = text[-end_limit:] if end_limit > 0 else ""
    return f"{start_part}...{end_part}"


def detect_primary_lang(text: str) -> str:
    """Detect whether text is primarily Chinese or English/other."""
    if not text:
        return "en"
    tokens = _CN_EN_NUM_RE.findall(text)
    if not tokens:
        return "en"
    cn_count = sum(1 for token in tokens if _CN_CHAR_RE.fullmatch(token))
    return "zh" if (cn_count / len(tokens)) >= CN_RATIO_THRESHOLD else "en"


def count_cn_en(text: str) -> int:
    """Count semantic Chinese/English/number tokens in a string."""
    return len(_CN_EN_NUM_RE.findall(str(text)))


def truncate_text_by_tokens(
    text: str,
    start_limit: int,
    end_limit: int,
    lang_aware: bool = True,
) -> str:
    """Truncate text by semantic token count, preserving whole words."""
    text = str(text)
    matches = list(_CN_EN_NUM_RE.finditer(text))
    total = len(matches)

    if lang_aware and total > 0 and detect_primary_lang(text) == "en":
        start_limit = min(start_limit, EN_START_LIMIT)

    if total <= start_limit + end_limit:
        return text

    cut_start = matches[start_limit - 1].end() if start_limit > 0 else 0
    cut_end = matches[total - end_limit].start() if end_limit > 0 else len(text)
    if cut_start >= cut_end:
        return text
    return text[:cut_start] + "..." + text[cut_end:]
