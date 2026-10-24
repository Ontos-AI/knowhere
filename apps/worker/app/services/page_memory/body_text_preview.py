"""Bound the display preview without changing stored PAGE content."""

from collections.abc import Iterable


def build_body_text_preview(texts: Iterable[str]) -> str:
    return " ".join(" ".join(texts).split())[:180]
