"""Smoke: registered tools include outline/links/inspect; direct call still works."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import app.services.document_agent.tools as _tools  # noqa: F401
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.pdf_text import PageTextBands, strip_margin_text
from app.services.document_agent.registry import REGISTRY
from app.services.document_agent.state import ProfileBlackboard
from app.services.document_agent.tools.grep_text import grep_text
from app.services.document_agent.tools.inspect_pages import inspect_pages
from app.services.document_agent.tools.text_strip_margins import strip_footer


@pytest.fixture(autouse=True)
def _rebind_live_tool_imports() -> Iterator[None]:
    """Rebind after contract fixtures that clear ``app.*`` from ``sys.modules``."""
    global REGISTRY, ToolContext, ProfileBlackboard
    global PageTextBands, strip_margin_text, strip_footer, grep_text, inspect_pages

    import app.services.document_agent.tools as _live_tools  # noqa: F401
    from app.services.document_agent.manifest import ToolContext as live_tool_context
    from app.services.document_agent.pdf_text import PageTextBands as live_bands
    from app.services.document_agent.pdf_text import (
        strip_margin_text as live_strip_margin_text,
    )
    from app.services.document_agent.registry import REGISTRY as live_registry
    from app.services.document_agent.state import ProfileBlackboard as live_blackboard
    from app.services.document_agent.tools.grep_text import grep_text as live_grep_text
    from app.services.document_agent.tools.inspect_pages import (
        inspect_pages as live_inspect_pages,
    )
    from app.services.document_agent.tools.text_strip_margins import (
        strip_footer as live_strip_footer,
    )

    REGISTRY = live_registry
    ToolContext = live_tool_context
    ProfileBlackboard = live_blackboard
    PageTextBands = live_bands
    strip_margin_text = live_strip_margin_text
    strip_footer = live_strip_footer
    grep_text = live_grep_text
    inspect_pages = live_inspect_pages
    yield


def test_probe_and_inspect_registered() -> None:
    for name in (
        "probe.outline",
        "probe.links",
        "judge.toc_source",
        "inspect.pages",
        "ocr.pages",
        "grep.text",
        "text.strip_header",
        "text.strip_footer",
    ):
        assert REGISTRY.get(name) is not None, name


def test_inspect_pages_handler_is_same_callable() -> None:
    spec = REGISTRY.get("inspect.pages")
    assert spec is not None
    assert spec.handler is inspect_pages


def test_grep_text_normalizes_query_and_corpus_by_default() -> None:
    blackboard = ProfileBlackboard(page_count=2)
    blackboard.page_full_text_cache = {
        1: "Public\nDomain Manual",
        2: "附录\nA   OVERVIEW",
    }
    ctx = ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="grep-normalization",
        blackboard=blackboard,
        trace=None,
        settings={},
    )

    result = grep_text(
        ctx,
        {"query": "附录 A OVERVIEW", "start_page": 2, "end_page": 2},
    )

    assert result.status == "ok"
    assert result.payload["normalized_query"] == "附录a overview"
    assert result.payload["hit_page_count"] == 1
    assert result.payload["hit_pages"] == [2]


def test_grep_text_whole_line_rejects_body_substring_and_dedupes_pages() -> None:
    blackboard = ProfileBlackboard(page_count=2)
    blackboard.page_full_text_cache = {
        1: (
            "The street furniture with advertising program continues.\n"
            "Street Furniture with Advertising\n"
            "Street Furniture with Advertising"
        ),
        2: "No heading here",
    }
    ctx = ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="grep-whole-line",
        blackboard=blackboard,
        trace=None,
        settings={},
    )

    result = grep_text(
        ctx,
        {
            "query": "Street Furniture with Advertising",
            "whole_line": True,
        },
    )

    assert result.status == "ok"
    assert result.payload["whole_line"] is True
    assert result.payload["hit_count"] == 2
    assert result.payload["hit_page_count"] == 1
    assert result.payload["hit_pages"] == [1]


def test_strip_footer_updates_search_view_for_grep() -> None:
    blackboard = ProfileBlackboard(page_count=1)
    blackboard.page_full_text_cache = {
        1: PageTextBands(
            content="Section Start\nPublic Domain Manual",
            header="",
            footer="Public Domain Manual",
        )
    }
    ctx = ToolContext(
        pdf_path="/tmp/doc.pdf",
        job_id="strip-footer",
        blackboard=blackboard,
        trace=None,
        settings={},
    )

    before = grep_text(
        ctx, {"query": "Public Domain Manual", "start_page": 1, "end_page": 1}
    )
    assert before.status == "ok"
    assert before.payload["hit_count"] == 1

    strip = strip_footer(ctx, {"start_page": 1, "end_page": 1})
    assert strip.status == "ok"
    assert strip.payload["pages_updated"] == 1
    assert ctx.blackboard.page_text_search_view[1] == "Section Start\n"

    after = grep_text(
        ctx, {"query": "Public Domain Manual", "start_page": 1, "end_page": 1}
    )
    assert after.status == "ok"
    assert after.payload["hit_count"] == 0

    # Stored cache must remain untouched.
    assert blackboard.page_full_text_cache[1].content == "Section Start\nPublic Domain Manual"


def test_strip_margin_text_edge_aligned_not_first_occurrence() -> None:
    """M4: footer/header strip only the matching edge, not body duplicates."""
    body_and_footer = "Public Domain Manual\nSection body\nPublic Domain Manual"
    assert (
        strip_margin_text(body_and_footer, "Public Domain Manual", edge="footer")
        == "Public Domain Manual\nSection body\n"
    )
    body_and_header = "Running Header\nSection body\nRunning Header"
    assert (
        strip_margin_text(body_and_header, "Running Header", edge="header")
        == "\nSection body\nRunning Header"
    )


def test_openai_specs_removed() -> None:
    assert not hasattr(REGISTRY, "openai_specs")
