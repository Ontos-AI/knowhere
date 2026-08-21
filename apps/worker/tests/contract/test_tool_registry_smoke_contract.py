"""Smoke: registered tools include outline/links/inspect; direct call still works."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

import app.services.document_agent.tools as _tools  # noqa: F401
from app.services.document_agent.manifest import ToolContext
from app.services.document_agent.registry import REGISTRY
from app.services.document_agent.state import ProfileBlackboard
from app.services.document_agent.tools.grep_text import grep_text
from app.services.document_agent.tools.inspect_pages import inspect_pages


def test_probe_and_inspect_registered() -> None:
    for name in (
        "probe.outline",
        "probe.links",
        "judge.toc_source",
        "inspect.pages",
        "ocr.pages",
        "grep.text",
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


def test_openai_specs_removed() -> None:
    assert not hasattr(REGISTRY, "openai_specs")
