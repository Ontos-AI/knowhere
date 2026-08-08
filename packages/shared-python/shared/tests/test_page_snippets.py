"""Tests for retrieval page-snippet extraction."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.retrieval.hydration.page_snippets import extract_page_snippets


def test_extract_page_snippets_returns_all_occurrences() -> None:
    content = (
        "Name: Mr. HUI Kim\nPost: Deputy Commissioner\n"
        + "X" * 300
        + "\nCHEUNG Hon-lam Gordon\n2835 2147\n"
        + "Y" * 300
        + "\nYUEN Chun-cheung Gordon\n2835 2154\n"
    )

    snippets = extract_page_snippets(content, ["gordon"])

    assert len(snippets) == 2
    assert "CHEUNG Hon-lam Gordon" in snippets[0]
    assert "YUEN Chun-cheung Gordon" in snippets[1]


def test_extract_page_snippets_respects_max_snippets_cap() -> None:
    content = "\n".join(f"Row {i}: Gordon {i}" for i in range(50))

    snippets = extract_page_snippets(content, ["gordon"], max_snippets=3)

    assert len(snippets) == 3


def test_extract_page_snippets_is_case_insensitive_and_word_boundary_aware() -> None:
    content = (
        "Gordon here."
        + "X" * 300
        + " Gordonstoun is not a match."
        + "Y" * 300
        + " gordon again."
    )

    snippets = extract_page_snippets(content, ["gordon"])

    assert len(snippets) == 2
    assert "Gordonstoun" not in "".join(snippets)


def test_extract_page_snippets_returns_empty_without_hits() -> None:
    assert extract_page_snippets("Gordon only once", ["missing"]) == []
    assert extract_page_snippets("", ["gordon"]) == []
    assert extract_page_snippets("Gordon here", []) == []


def test_extract_page_snippets_deduplicates_identical_snippets() -> None:
    content = "Mr A Gordon" + "X" * 300 + "\nMr A Gordon"

    snippets = extract_page_snippets(content, ["gordon"])

    assert len(snippets) == 1


def test_extract_page_snippets_keeps_snippets_bounded() -> None:
    content = "A" * 1000 + " Gordon " + "B" * 1000

    snippets = extract_page_snippets(content, ["gordon"], context_chars=100)

    assert len(snippets) == 1
    assert len(snippets[0]) <= 220
    assert snippets[0].startswith("…")
    assert snippets[0].endswith("…")
