"""Evidence pool rounds / issue / pick / render / compose."""

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
    PickOutcome,
    compose_pool_evidence,
    render_budget,
    render_pick_outcome,
    render_trace_line,
    trace_status,
)
from shared.services.retrieval.agent_tools import Decision, ToolResult
from shared.services.retrieval.hydration.evidence_compose import compose_evidence_parts
from shared.services.retrieval.agent_tools.section_path_lookup import (
    section_path_received,
)


def _ok_read(*, refs: list[dict], chunks: list[dict]) -> ToolResult:
    return ToolResult(text="read", payload={"refs": refs, "chunks": chunks})


def _read_one(section_path: str, chunk_ids: list[str]) -> ToolResult:
    return _ok_read(
        refs=[
            {
                "status": "ok",
                "document_id": "doc_a",
                "section_path": section_path,
                "chunk_ids": chunk_ids,
            }
        ],
        chunks=[
            {
                "chunk_id": chunk_id,
                "document_id": "doc_a",
                "source_file_name": "guide.pdf",
                "section_path": section_path,
                "chunk_type": "text",
                "section_summary": section_path,
            }
            for chunk_id in chunk_ids
        ],
    )


def _outline() -> ToolResult:
    return ToolResult(
        text="outline",
        payload={
            "rows": [
                {
                    "document_id": "doc_a",
                    "section_path": "guide.pdf / 1 Overview",
                    "title": "1 Overview",
                    "depth": 0,
                }
            ],
            "details": {"documents": {"doc_a": "guide.pdf"}},
        },
    )


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


def test_ids_issued_this_round_take_effect_next_round() -> None:
    pool = EvidencePool()
    pool.begin_round()
    pool.issue("corpus.read", _read_one("A", ["c1"]))
    assert pool.pick_phase is False
    same_round = pool.apply_pick(["R1.1"])
    assert same_round.added == []
    assert same_round.rejected == [{"handle": "R1.1", "reason": "unknown id"}]

    pool.begin_round()
    assert pool.round_index == 2
    assert pool.pick_phase is True
    next_round = pool.apply_pick(["R1.1"])
    assert next_round.added == ["R1.1"]


def test_pick_phase_lasts_until_pick_is_called() -> None:
    pool = EvidencePool()
    pool.begin_round()
    pool.issue("corpus.outline", _outline())
    pool.begin_round()
    assert pool.pick_phase is True
    pool.begin_round()
    assert pool.pick_phase is True
    pool.apply_pick([])
    assert pool.pick_phase is False
    pool.begin_round()
    assert pool.pick_phase is False


def test_apply_pick_rejects_unknown_duplicate_and_already_in_pool() -> None:
    pool = EvidencePool()
    pool.begin_round()
    pool.issue(
        "corpus.read",
        _ok_read(
            refs=[
                {
                    "status": "ok",
                    "document_id": "doc_a",
                    "section_path": "A",
                    "chunk_ids": ["c1"],
                },
                {
                    "status": "ok",
                    "document_id": "doc_a",
                    "section_path": "A",
                    "chunk_ids": ["c1"],
                },
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
    pool.begin_round()
    outcome = pool.apply_pick(["R1.1", "R1.1", "R9.9", "R1.2"])
    assert outcome.added == ["R1.1"]
    assert outcome.rejected == [
        {"handle": "R9.9", "reason": "unknown id"},
        {"handle": "R1.2", "reason": "already in pool"},
    ]


def test_unpicked_read_chunks_are_decided_and_unpicked_outline_is_dropped() -> None:
    pool = EvidencePool()
    pool.begin_round()
    pool.issue("corpus.read", _read_one("A", ["c1", "c2"]))
    pool.issue("corpus.read", _read_one("B", ["c3"]))
    pool.issue("corpus.outline", _outline())
    pool.begin_round()
    outcome = pool.apply_pick(["R2.1"])
    assert outcome.added == ["R2.1"]
    assert [item.handle for item in pool.entries] == ["R2.1"]
    assert dict(pool.decided()) == {
        ("doc_a", "c1"): Decision(read_round=1, picked_handle=None, handle="R1.1"),
        ("doc_a", "c2"): Decision(read_round=1, picked_handle=None, handle="R1.1"),
        ("doc_a", "c3"): Decision(read_round=1, picked_handle="R2.1", handle="R2.1"),
    }
    pool.begin_round()
    assert pool.pick_phase is False
    assert pool.apply_pick(["O1"]).rejected == [{"handle": "O1", "reason": "unknown id"}]


def test_seen_addresses_become_readable_when_next_round_begins() -> None:
    pool = EvidencePool()
    pool.begin_round()
    pool.issue(
        "corpus.grep",
        ToolResult(
            text="hits",
            payload={
                "rows": [
                    {
                        "document_id": "doc_a",
                        "section_path": "guide.pdf / 2 Treatment",
                        "chunk_id": None,
                        "mounted_chunk_ids": ["tbl_1"],
                    },
                    {
                        "document_id": "doc_a",
                        "section_path": "guide.pdf / Root",
                        "chunk_id": "img_1",
                        "mounted_chunk_ids": [],
                    },
                ]
            },
        ),
    )
    pool.issue(
        "corpus.read",
        _ok_read(
            refs=[
                {
                    "status": "ok",
                    "document_id": "doc_b",
                    "section_path": "notes.pdf / Intro",
                    "chunk_ids": ["c9"],
                }
            ],
            chunks=[
                {
                    "chunk_id": "c9",
                    "document_id": "doc_b",
                    "section_path": "notes.pdf / Intro",
                    "chunk_type": "text",
                    "chunk_metadata": {"connect_to": [{"target": "img_9"}]},
                }
            ],
        ),
    )
    assert pool.readable() == frozenset()
    pool.begin_round()
    assert pool.readable() == {
        ("doc_a", "guide.pdf / 2 Treatment"),
        ("doc_a", "tbl_1"),
        ("doc_a", "guide.pdf / Root"),
        ("doc_a", "img_1"),
        ("doc_b", "notes.pdf / Intro"),
        ("doc_b", "c9"),
        ("doc_b", "img_9"),
    }


def test_section_path_received_accepts_ancestor_and_suffix_paths() -> None:
    readable = {("doc_a", "guide.pdf / 1 Overview / 1.1 Findings")}
    assert section_path_received(readable, "doc_a", "guide.pdf / 1 Overview")
    assert section_path_received(readable, "doc_a", "1 Overview / 1.1 Findings")
    assert section_path_received(readable, "doc_a", "1.1 Findings")
    assert not section_path_received(readable, "doc_a", "guide.pdf / 1.1 Findings")
    assert not section_path_received(readable, "doc_a", "2 Treatment")
    assert not section_path_received(readable, "doc_b", "1 Overview")


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
    pool.begin_round()
    pool.apply_pick(["O1"])
    text = pool.render_pool()
    assert text.startswith("evidence pool (1):")
    assert "  [O1] Hypertension_Guideline.pdf" in text
    assert "  1 Overview" in text
    assert pool.render_candidates(outline) == "[pick id for this result: O1 = this outline]"


def test_render_budget_and_trace_line_ok_partial_failed() -> None:
    budget = EpisodeBudget(max_steps=12, wall_clock_seconds=180)
    budget.record_step()
    budget.record_usage({"total_tokens": 12034})
    assert render_budget(budget) == "budget: steps 1/12"
    assert budget.tokens_used == 12034

    ok = render_trace_line(
        1,
        "corpus_outline",
        {"scope": [{"document_id": "doc_a"}]},
        trace_status(ToolResult(text="ok")),
    )
    assert ok == '1. corpus_outline {"scope": [{"document_id": "doc_a"}]} -> ok'

    failed = render_trace_line(
        3,
        "corpus_grep",
        {"patterns": ["ACEI"]},
        trace_status(
            ToolResult(text="", error="corpus.grep: unknown argument(s) ['max_results']")
        ),
    )
    assert failed.endswith(" -> failed: corpus.grep: unknown argument(s) ['max_results']")

    partial = render_trace_line(
        2,
        "corpus_read",
        {"refs": [{"document_id": "doc_a", "section_path": "A"}]},
        trace_status(
            ToolResult(
                text="partial",
                payload={
                    "refs": [
                        {"status": "ok"},
                        {"status": "failed", "reason": "unknown section_path for doc_a"},
                    ]
                },
            )
        ),
    )
    assert " -> partial: ref 2 failed: unknown section_path for doc_a" in partial


def test_render_pick_outcome() -> None:
    assert render_pick_outcome(PickOutcome()) == "picked: none"
    assert (
        render_pick_outcome(
            PickOutcome(
                added=["R1.1", "O2"],
                rejected=[{"handle": "R9.9", "reason": "unknown id"}],
            )
        )
        == "picked: R1.1, O2; rejected: R9.9 (unknown id)"
    )


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
            "document_id": "doc_a",
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


def test_query_table_records_last_successful_html_and_issues_nothing() -> None:
    pool = EvidencePool()
    first = ToolResult(
        text="first",
        payload={
            "document_id": "doc_a",
            "chunk_id": "t1",
            "table_html": "<table>first</table>",
        },
    )
    second = ToolResult(
        text="second",
        payload={
            "document_id": "doc_a",
            "chunk_id": "t1",
            "table_html": "<table>second</table>",
        },
    )
    failed = ToolResult(
        text="",
        error="bad sql",
        payload={
            "document_id": "doc_a",
            "chunk_id": "t1",
            "table_html": "<table>failed</table>",
        },
    )
    other_doc = ToolResult(
        text="other",
        payload={
            "document_id": "doc_b",
            "chunk_id": "t1",
            "table_html": "<table>other</table>",
        },
    )
    assert pool.issue("corpus.query_table", first) == []
    assert pool.issue("corpus.query_table", second) == []
    assert pool.issue("corpus.query_table", failed) == []
    assert pool.issue("corpus.query_table", other_doc) == []
    assert pool.issue("corpus.grep", ToolResult(text="hits")) == []
    assert dict(pool.queried_tables()) == {
        ("doc_a", "t1"): "<table>second</table>",
        ("doc_b", "t1"): "<table>other</table>",
    }
    assert pool.entries == []


def test_queried_table_reaches_pool_evidence() -> None:
    pool = EvidencePool()
    pool.issue(
        "corpus.query_table",
        ToolResult(
            text="ok",
            payload={
                "document_id": "doc_a",
                "chunk_id": "c1",
                "table_html": "<table><tr><td>sub</td></tr></table>",
            },
        ),
    )
    parts = compose_evidence_parts(
        {
            "document_id": "doc_a",
            "chunk_id": "c1",
            "chunk_type": "table",
            "content": "<table><tr><td>FULL</td></tr></table>",
        },
        {},
        queried_tables=pool.queried_tables(),
    )
    evidence = compose_pool_evidence(
        [
            Candidate(
                handle="R1.1",
                kind="read",
                document_id="doc_a",
                source_file_name="guide.pdf",
                section_path="2 Treatment",
                chunk_ids=("c1",),
            )
        ],
        [{"document_id": "doc_a", "chunk_id": "c1", "sort_order": 1, "composed": parts}],
    )
    text = "".join(part["text"] for part in evidence if part["type"] == "text")
    assert "sub" in text
    assert "FULL" not in text


def test_query_table_without_table_html_is_not_recorded() -> None:
    pool = EvidencePool()
    pool.issue(
        "corpus.query_table",
        ToolResult(
            text="ok",
            payload={
                "document_id": "doc_a",
                "chunk_id": "t1",
                "headers": ["Unit"],
                "rows": [["tablet"]],
            },
        ),
    )
    assert dict(pool.queried_tables()) == {}
