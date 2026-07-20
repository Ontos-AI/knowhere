"""Tests for hierarchy path segment escaping (titles that contain ``/``)."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.chunks.document_path import split_document_path
from shared.services.chunks.path_segments import (
    ESCAPED_DOCUMENT_PATH_SEP,
    append_document_path,
    escape_path_segment,
    join_document_path,
    split_escaped_document_path,
    unescape_path_segment,
)
from shared.services.retrieval.search.lexical_text import (
    section_path_from_chunk_path,
    split_section_path,
)
from shared.services.storage.zip_doc_navigation import ZipDocNavigationBuilder


def test_escape_round_trip_for_slash_in_title() -> None:
    title = "Symbols/Numbers"
    escaped = escape_path_segment(title)
    assert ESCAPED_DOCUMENT_PATH_SEP in escaped
    assert "/" not in escaped
    assert unescape_path_segment(escaped) == title


def test_escape_round_trip_for_literal_escape_char() -> None:
    title = f"A{ESCAPED_DOCUMENT_PATH_SEP}B/C"
    assert unescape_path_segment(escape_path_segment(title)) == title


def test_join_and_split_keeps_slash_title_as_one_segment() -> None:
    path = join_document_path(["manual.pdf", "Index", "Symbols/Numbers", "Detail"])
    assert path == (
        f"manual.pdf/Index/Symbols{ESCAPED_DOCUMENT_PATH_SEP}Numbers/Detail"
    )
    assert split_escaped_document_path(path) == [
        "manual.pdf",
        "Index",
        "Symbols/Numbers",
        "Detail",
    ]


def test_append_under_escaped_parent() -> None:
    parent = join_document_path(["manual.pdf", "Index"])
    child = append_document_path(parent, "Symbols/Numbers")
    assert split_escaped_document_path(child)[-1] == "Symbols/Numbers"
    assert child.count("/") == 2


def test_split_document_path_unescapes_section_titles() -> None:
    chunk_path = join_document_path(
        ["manual.pdf", "Index", "Symbols/Numbers"]
    )
    root_parts, section_parts = split_document_path(
        chunk_path,
        source_file_name="manual.pdf",
    )
    assert root_parts == ["manual.pdf"]
    assert section_parts == ["Index", "Symbols/Numbers"]


def test_section_path_from_chunk_path_preserves_slash_title() -> None:
    chunk_path = join_document_path(
        ["manual.pdf", "Index", "Symbols/Numbers"]
    )
    assert (
        section_path_from_chunk_path(
            chunk_path,
            source_file_name="manual.pdf",
        )
        == "Index / Symbols/Numbers"
    )
    assert split_section_path("Index / Symbols/Numbers") == [
        "Index",
        "Symbols/Numbers",
    ]


def test_doc_nav_keeps_slash_title_as_single_section() -> None:
    chunk_path = join_document_path(
        ["manual.pdf", "Index", "Symbols/Numbers"]
    )
    doc_nav = ZipDocNavigationBuilder().build_doc_nav(
        [
            {
                "chunk_id": "chunk_slash_title",
                "type": "text",
                "content": "index symbols",
                "path": chunk_path,
                "metadata": {"summary": "symbols"},
            }
        ],
        "manual.pdf",
    )
    sections = doc_nav["sections"]
    assert sections[0]["title"] == "Index"
    assert sections[0]["children"][0]["title"] == "Symbols/Numbers"
    assert sections[0]["children"][0]["path"] == chunk_path
    assert sections[0]["children"][0]["children"] == []
