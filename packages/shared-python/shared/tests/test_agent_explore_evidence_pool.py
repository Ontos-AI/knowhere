"""Evidence pool issue / pick / render / compose."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("TMP_PATH", "/tmp/knowhere-test")
os.environ.setdefault("S3_BUCKET_NAME", "test-uploads")
os.environ.setdefault("S3_ACCESS_KEY_ID", "test")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_TEMP_PATH", "/tmp")

from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_explore.evidence_pool import (
    Candidate,
    EvidencePool,
    compose_pool_evidence,
    render_budget,
    render_trace_line,
)
from shared.services.retrieval.agent_tools import ToolResult


def _ok_read(*, refs: list[dict], chunks: list[dict]) -> ToolResult:
    return ToolResult(text="read", payload={"refs": refs, "chunks": chunks})


def test_issue_read_only_numbers_successful_refs_at_original_index() -> None:
    pool = EvidencePool()
    result = _ok_read(
        refs=[
            {
                "status": "failed",
                "document_id": "doc_a",
                "section_path": "missing",
                "chunk_ids": [],
            },
            {
                "status": "ok",
                "document_id": "doc_a",
                "section_path": "2 Treatment",
                "chunk_ids": ["c2"],
            },
        ],
        chunks=[
            {
                "chunk_id": "c2",
                "document_id": "doc_a",
                "source_file_name": "guide.pdf",
                "section_path": "2 Treatment",
                "chunk_type": "text",
                "section_summary": "First-line drugs",
            }
        ],
    )
    issued = pool.issue("corpus.read", result)
    assert [item.handle for item in issued] == ["R1.2"]
    assert issued[0].summary == "First-line drugs"
    assert issued[0].chunk_ids == ("c2",)


def test_take_pending_expires_unpicked_handles() -> None:
    pool = EvidencePool()
    pool.issue(
        "corpus.read",
        _ok_read(
            refs=[
                {
                    "status": "ok",
                    "document_id": "doc_a",
                    "section_path": "A",
                    "chunk_ids": ["c1"],
                }
            ],
            chunks=[
                {
                    "chunk_id": "c1",
                    "document_id": "doc_a",
                    "source_file_name": "guide.pdf",
                    "section_path": "A",
                    "chunk_type": "text",
                    "section_summary": "A",
                }
            ],
        ),
    )
    pending = pool.take_pending()
    assert "R1.1" in pending
    outcome = pool.apply_pick(["R1.1"], pool.take_pending())
    assert outcome.added == []
    assert outcome.rejected == [{"handle": "R1.1", "reason": "expired or unknown"}]


def test_apply_pick_rejects_unknown_duplicate_and_already_in_pool() -> None:
    pool = EvidencePool()
    pool.issue(
        "corpus.read",
        _ok_read(
            refs=[
                {
                    "status": "ok",
                    "document_id": "doc_a",
                    "section_path": "A",
                    "chunk_ids": ["c1"],
                }
            ],
            chunks=[
                {
                    "chunk_id": "c1",
                    "document_id": "doc_a",
                    "source_file_name": "guide.pdf",
                    "section_path": "A",
                    "chunk_type": "text",
                    "section_summary": "A",
                }
            ],
        ),
    )
    pending = pool.take_pending()
    first = pool.apply_pick(["R1.1", "R1.1", "R9.9"], pending)
    assert first.added == ["R1.1"]
    assert {"handle": "R9.9", "reason": "expired or unknown"} in first.rejected

    pool.issue(
        "corpus.read",
        _ok_read(
            refs=[
                {
                    "status": "ok",
                    "document_id": "doc_a",
                    "section_path": "A",
                    "chunk_ids": ["c1"],
                }
            ],
            chunks=[
                {
                    "chunk_id": "c1",
                    "document_id": "doc_a",
                    "source_file_name": "guide.pdf",
                    "section_path": "A",
                    "chunk_type": "text",
                    "section_summary": "A",
                }
            ],
        ),
    )
    second = pool.apply_pick(["R2.1"], pool.take_pending())
    assert second.added == []
    assert second.rejected == [{"handle": "R2.1", "reason": "already in pool"}]


def test_outline_tree_is_titles_and_indent_only() -> None:
    pool = EvidencePool()
    issued = pool.issue(
        "corpus.outline",
        ToolResult(
            text="outline",
            payload={
                "rows": [
                    {
                        "document_id": "doc_a",
                        "title": "1 Overview",
                        "depth": 0,
                    },
                    {
                        "document_id": "doc_a",
                        "title": "2 Treatment",
                        "depth": 0,
                    },
                    {
                        "document_id": "doc_a",
                        "title": "2.1 Lifestyle",
                        "depth": 1,
                    },
                ],
                "details": {"documents": {"doc_a": "Hypertension_Guideline.pdf"}},
            },
        ),
    )
    assert issued[0].handle == "O1"
    assert issued[0].outline_lines == (
        "  [O1] Hypertension_Guideline.pdf",
        "  1 Overview",
        "  2 Treatment",
        "    2.1 Lifestyle",
    )
    rendered = pool.render_candidates(issued)
    assert rendered == "[pick id for this result: O1 = this outline]"


def test_descendants_keeps_anchor_summary_and_extra_sections() -> None:
    pool = EvidencePool()
    issued = pool.issue(
        "corpus.read",
        _ok_read(
            refs=[
                {
                    "status": "ok",
                    "document_id": "doc_a",
                    "section_path": "2 Treatment",
                    "chunk_ids": ["c_anchor", "c_child"],
                }
            ],
            chunks=[
                {
                    "chunk_id": "c_anchor",
                    "document_id": "doc_a",
                    "source_file_name": "guide.pdf",
                    "section_path": "2 Treatment",
                    "chunk_type": "text",
                    "section_summary": "anchor summary",
                },
                {
                    "chunk_id": "c_child",
                    "document_id": "doc_a",
                    "source_file_name": "guide.pdf",
                    "section_path": "2 Treatment / 2.1 Lifestyle",
                    "chunk_type": "text",
                    "section_summary": "child summary",
                },
            ],
        ),
    )
    assert issued[0].summary == "anchor summary"
    assert issued[0].extra_sections == 1
    assert issued[0].chunk_ids == ("c_anchor", "c_child")


def test_render_pool_and_empty() -> None:
    pool = EvidencePool()
    assert pool.render_pool() == "evidence pool: empty"
    outline = pool.issue(
        "corpus.outline",
        ToolResult(
            text="outline",
            payload={
                "rows": [{"document_id": "doc_a", "title": "1 Overview", "depth": 0}],
                "details": {"documents": {"doc_a": "Hypertension_Guideline.pdf"}},
            },
        ),
    )
    pool.apply_pick(["O1"], pool.take_pending())
    text = pool.render_pool()
    assert text.startswith("evidence pool (1):")
    assert "  [O1] Hypertension_Guideline.pdf" in text
    assert "  1 Overview" in text
    assert pool.render_candidates(outline) == "[pick id for this result: O1 = this outline]"


def test_render_budget_and_trace_line_ok_partial_failed() -> None:
    budget = EpisodeBudget(token_limit=100000, max_steps=12, wall_clock_seconds=180)
    budget.record_step()
    budget.record_usage({"total_tokens": 12034})
    line = render_budget(budget)
    assert line.startswith("budget: steps 1/12, tokens 12034/100000, elapsed ")
    assert line.endswith("/180s")

    ok = render_trace_line(1, "corpus_outline", {"scope": [{"document_id": "doc_a"}]}, ToolResult(text="ok"))
    assert ok == '1. corpus_outline {"scope": [{"document_id": "doc_a"}]} -> ok'

    failed = render_trace_line(
        3,
        "corpus_grep",
        {"patterns": ["ACEI"]},
        ToolResult(text="", error="corpus.grep: unknown argument(s) ['max_results']"),
    )
    assert failed.endswith(" -> failed: corpus.grep: unknown argument(s) ['max_results']")

    partial = render_trace_line(
        2,
        "corpus_read",
        {"refs": [{"document_id": "doc_a", "section_path": "A"}]},
        ToolResult(
            text="partial",
            payload={
                "refs": [
                    {"status": "ok"},
                    {"status": "failed", "reason": "unknown section_path for doc_a"},
                ]
            },
        ),
    )
    assert " -> partial: ref 2 failed: unknown section_path for doc_a" in partial


def test_compose_pool_evidence_keeps_pick_order_and_outline_fragment() -> None:
    entries = [
        Candidate(
            handle="O1",
            kind="outline",
            document_id="doc_a",
            source_file_name="guide.pdf",
            outline_lines=("  [O1] guide.pdf", "  1 Overview"),
        ),
        Candidate(
            handle="R2.1",
            kind="read",
            document_id="doc_a",
            source_file_name="guide.pdf",
            section_path="2 Treatment",
            chunk_ids=("c1", "missing"),
        ),
    ]
    assembled = [
        {
            "chunk_id": "c1",
            "sort_order": 4,
            "composed": [{"type": "text", "text": "treatment body"}],
        }
    ]
    evidence = compose_pool_evidence(entries, assembled)
    assert evidence == [
        {"type": "text", "text": "[E1] [§ guide.pdf]"},
        {"type": "text", "text": "    [O1] guide.pdf\n    1 Overview"},
        {"type": "text", "text": "\n"},
        {"type": "text", "text": "[E2] [§ guide.pdf / 2 Treatment]"},
        {"type": "text", "text": "  treatment body"},
    ]


def test_issue_ignores_other_tools_and_errors() -> None:
    pool = EvidencePool()
    assert pool.issue("corpus.grep", ToolResult(text="hits")) == []
    assert pool.issue("corpus.read", ToolResult(text="", error="bad args")) == []
    assert pool.issue("corpus.assets", ToolResult(text="assets")) == []
