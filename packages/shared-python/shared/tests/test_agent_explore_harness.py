"""Unit tests for the Phase 3.5 ``agent_explore`` harness layer.

Covers only the pure functions and the ``AGENT_EXPLORE_HARNESS`` switch
resolution — none of these need a real DB or LLM. Does not cover
``dispatch.dispatch_tool_call`` (needs a DB session) or a full
``Harness.run_episode`` loop (needs an LLM/Cursor SDK backend) — those stay
integration-level, exercised via the debug scripts and
``eval-cursor-harness``, not here.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import pytest

from shared.services.retrieval.agent_explore.harness.base import Harness
from shared.services.retrieval.agent_explore.harness.resolve import (
    _HARNESS_ENV,
    resolve_harness,
    resolve_harness_name,
)
from shared.services.retrieval.agent_explore.shared import (
    EVIDENCE_TOOL_NAMES,
    build_wire_tool_name_map,
    dedup_refs,
    normalize_finish_refs,
    tool_message_content,
    wire_safe_tool_name,
)
from shared.services.retrieval.agent_tools import ToolResult


# --------------------------------------------------------------------------
# shared.py: wire-safe tool name mapping
# --------------------------------------------------------------------------


def test_wire_safe_tool_name_replaces_dots() -> None:
    assert wire_safe_tool_name("corpus.read") == "corpus_read"
    assert wire_safe_tool_name("corpus.node_filter") == "corpus_node_filter"
    # Already wire-safe names are untouched.
    assert wire_safe_tool_name("finish") == "finish"


def test_build_wire_tool_name_map_round_trips_to_canonical() -> None:
    mapping = build_wire_tool_name_map(["corpus.read", "corpus.recall", "finish"])
    assert mapping == {
        "corpus_read": "corpus.read",
        "corpus_recall": "corpus.recall",
        "finish": "finish",
    }


# --------------------------------------------------------------------------
# shared.py: tool_message_content (max_chars capping)
# --------------------------------------------------------------------------


def test_tool_message_content_passes_through_short_text() -> None:
    result = ToolResult(text="short body")
    assert tool_message_content(result, max_chars=100) == "short body"


def test_tool_message_content_caps_long_text_with_note() -> None:
    result = ToolResult(text="x" * 200)
    content = tool_message_content(result, max_chars=100)
    assert content.startswith("x" * 100)
    assert "truncated, 100 more chars" in content
    assert len(content) > 100  # capped body + truncation note, not silently dropped


def test_tool_message_content_surfaces_error_instead_of_text() -> None:
    result = ToolResult(text="ignored", error="bad args: missing document_id")
    assert tool_message_content(result, max_chars=100) == "error: bad args: missing document_id"


def test_tool_message_content_empty_text_placeholder() -> None:
    result = ToolResult(text="")
    assert tool_message_content(result, max_chars=100) == "(empty result)"


# --------------------------------------------------------------------------
# shared.py: normalize_finish_refs / dedup_refs
# --------------------------------------------------------------------------


def test_normalize_finish_refs_drops_non_dict_and_empty_document_id() -> None:
    raw = [
        {"document_id": "doc_a", "chunk_id": "c1"},
        {"document_id": "", "chunk_id": "c2"},
        {"chunk_id": "c3"},
        "not a dict",
        None,
        {"document_id": "doc_b"},
    ]
    normalized = normalize_finish_refs(raw)
    assert normalized == [
        {"document_id": "doc_a", "chunk_id": "c1"},
        {"document_id": "doc_b"},
    ]


def test_normalize_finish_refs_non_list_input_is_empty() -> None:
    assert normalize_finish_refs(None) == []
    assert normalize_finish_refs("refs") == []
    assert normalize_finish_refs({"document_id": "doc_a"}) == []


def test_dedup_refs_keeps_first_seen_and_drops_missing_ids() -> None:
    refs = [
        {"document_id": "doc_a", "chunk_id": "c1", "section_path": "first"},
        {"document_id": "doc_a", "chunk_id": "c1", "section_path": "duplicate"},
        {"document_id": "doc_a", "chunk_id": "c2"},
        {"document_id": "doc_a"},  # missing chunk_id -> dropped
        {"chunk_id": "c3"},  # missing document_id -> dropped
        {"document_id": "doc_b", "chunk_id": "c1"},
    ]
    deduped = dedup_refs(refs)
    assert deduped == [
        {"document_id": "doc_a", "chunk_id": "c1", "section_path": "first"},
        {"document_id": "doc_a", "chunk_id": "c2"},
        {"document_id": "doc_b", "chunk_id": "c1"},
    ]


def test_evidence_tool_names_is_read_and_assets_only() -> None:
    assert EVIDENCE_TOOL_NAMES == frozenset({"corpus.read", "corpus.assets"})


# --------------------------------------------------------------------------
# harness/resolve.py: AGENT_EXPLORE_HARNESS switch
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_harness_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_HARNESS_ENV, raising=False)


def test_resolve_harness_name_defaults_to_openai() -> None:
    assert resolve_harness_name() == "openai"


def test_resolve_harness_name_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_HARNESS_ENV, "cursor_sdk")
    assert resolve_harness_name() == "cursor_sdk"


def test_resolve_harness_name_unknown_value_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_HARNESS_ENV, "not_a_real_harness")
    assert resolve_harness_name() == "openai"


def test_resolve_harness_name_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_HARNESS_ENV, "CURSOR_SDK")
    assert resolve_harness_name() == "cursor_sdk"


def test_resolve_harness_default_builds_openai_harness() -> None:
    from shared.services.retrieval.agent_explore.harness.openai_harness import OpenAIHarness

    harness = resolve_harness()
    assert isinstance(harness, OpenAIHarness)
    assert isinstance(harness, Harness)


def test_resolve_harness_cursor_sdk_builds_cursor_harness_without_sdk_installed() -> None:
    """Selecting cursor_sdk must not require the optional cursor-sdk package
    to be importable — only actually running an episode does (see
    cursor_harness.py's guarded _require_cursor_sdk, exercised at
    run_episode() call time, not at harness construction time).
    """
    from shared.services.retrieval.agent_explore.harness.cursor_harness import CursorHarness

    harness = resolve_harness("cursor_sdk")
    assert isinstance(harness, CursorHarness)
    assert isinstance(harness, Harness)


def test_resolve_harness_explicit_name_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.services.retrieval.agent_explore.harness.openai_harness import OpenAIHarness

    monkeypatch.setenv(_HARNESS_ENV, "cursor_sdk")
    harness = resolve_harness("openai")
    assert isinstance(harness, OpenAIHarness)
