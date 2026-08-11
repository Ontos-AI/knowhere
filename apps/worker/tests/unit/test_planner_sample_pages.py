"""Coarse planner page sampling."""

from __future__ import annotations

import random

from app.services.document_agent.planner.planner import _sample_pages


def test_sample_pages_with_extrema_keeps_extrema_first() -> None:
    pages = _sample_pages(273, [1, 270], rng=random.Random(0))
    assert pages[0] == 1
    assert 270 in pages
    assert len(pages) <= 10


def test_zero_text_skips_extrema_and_adds_one_random() -> None:
    """When caller drops extrema (max visible text == 0), add one random page."""
    rng = random.Random(0)
    pages = _sample_pages(273, [], random_extra=1, rng=rng)

    # Stratified front/mid/back only (no extrema): first/last of each third.
    # pool=[1..273], third=91 → [1,91] + [92,182] + [183,273] + 1 random.
    assert pages[:6] == [1, 91, 92, 182, 183, 273]
    assert len(pages) == 7
    assert pages[6] not in {1, 91, 92, 182, 183, 273}
    assert 1 <= pages[6] <= 273


def test_zero_text_random_extra_is_deterministic_with_rng() -> None:
    a = _sample_pages(273, [], random_extra=1, rng=random.Random(7))
    b = _sample_pages(273, [], random_extra=1, rng=random.Random(7))
    c = _sample_pages(273, [], random_extra=1, rng=random.Random(8))
    assert a == b
    assert a != c
