"""Token estimation helpers for retrieval budgeting.

The estimator intentionally keeps ``tiktoken`` optional.  Production
environments that install it get model-aware counts; other environments use a
conservative mixed Chinese/English heuristic with no extra dependency.
"""
from __future__ import annotations

import re
from functools import lru_cache


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


@lru_cache(maxsize=32)
def _get_tiktoken_encoding(model_hint: str | None):
    try:
        import tiktoken  # type: ignore[import-not-found]
    except Exception:
        return None

    try:
        if model_hint:
            return tiktoken.encoding_for_model(model_hint)
    except Exception:
        # Model hint lookup failed; fall through to cl100k_base default
        pass

    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _heuristic_estimate(text: str) -> int:
    if not text:
        return 0

    zh_chars = len(_CJK_RE.findall(text))
    ascii_chars = sum(len(match.group(0)) for match in _ASCII_WORD_RE.finditer(text))
    other_chars = max(len(text) - zh_chars - ascii_chars, 0)

    mixed_estimate = (zh_chars / 1.5) + (ascii_chars / 4.0) + (other_chars / 3.0)
    conservative_floor = len(text) / 2.5
    return max(1, int(max(mixed_estimate, conservative_floor)))


def estimate_tokens(text: str, model_hint: str | None = None) -> int:
    """Estimate input tokens for ``text``.

    ``model_hint`` is advisory.  If no compatible tokenizer is available, the
    function falls back to a deterministic heuristic.
    """
    if not text:
        return 0

    encoding = _get_tiktoken_encoding(model_hint)
    if encoding is not None:
        try:
            return len(encoding.encode(text))
        except Exception:
            pass

    return _heuristic_estimate(text)
