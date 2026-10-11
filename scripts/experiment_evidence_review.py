"""Paired evidence-review runtime experiment over a frozen synthetic corpus.

This runs the production OpenAIHarness and EvidenceReviewSession. Only the
explorer transport and corpus backend are scripted; --mode live-reviewer also
uses the production reviewer transport. It is not a production retrieval or
answer-quality benchmark. See docs/evidence-sufficiency-experiment.md.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "scripts/fixtures/evidence_review_cases.json"
FINISH_NOTE = "EXPERIMENTER_FINISH_NOTE_SHOULD_NOT_BE_SOURCE"
EXPLORER_TOKENS = 13
REVIEWER_TOKENS = 47
LIVE_EXCLUDED = {
    "reviewer_outage", "malformed_verdict", "false_sufficiency_negative_control"
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def prepare_imports() -> None:
    """Satisfy settings validation without opening a DB or storage connection."""
    sys.path.insert(0, str(ROOT / "packages/shared-python"))
    for key, value in {
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
        "TMP_PATH": "/tmp/knowhere-evidence-experiment",
        "S3_BUCKET_NAME": "test-uploads",
        "S3_ACCESS_KEY_ID": "test",
        "S3_SECRET_ACCESS_KEY": "test",
        "S3_TEMP_PATH": "/tmp",
    }.items():
        os.environ.setdefault(key, value)


class FrozenCorpus:
    """Scripted discovery pages, with source-only reads from the same corpus.

    Neither the explorer nor this backend sees gold labels. The second page is
    exposed only when the real finish gate resumes the explorer. This models
    a controlled recovery opportunity, not actual search ranking quality.
    """

    def __init__(self, case: dict[str, Any]) -> None:
        self.query = case["query"]
        self.document_id = f"doc-{case['case_id']}"
        self.file_name = f"{case['case_id']}.md"
        self.sources = {row["chunk_id"]: row for row in case["corpus"]}
        self.pages = case["retrieval_pages"]
        self.discoveries = 0
        self.reads = 0
        self.last_refs: list[dict[str, str]] = []
        self.pending_handles: list[str] = []
        self.snapshot_sources: list[list[str]] = []
        self.latest_snapshot: Any = None
        self.dispatches: list[str] = []

    def row(self, chunk_id: str) -> dict[str, Any]:
        source = self.sources[chunk_id]
        return {
            "chunk_id": chunk_id,
            "document_id": self.document_id,
            "source_file_name": self.file_name,
            "section_path": f"{self.file_name} / {chunk_id}",
            "chunk_type": source["chunk_type"],
            "content": source["text"],
            "section_summary": source.get("summary", ""),
            "chunk_metadata": {},
            "job_result_id": "frozen-fixture-v1",
            "sort_order": list(self.sources).index(chunk_id),
        }

    async def dispatch(
        self, name: str, args: dict[str, Any], **kwargs: Any
    ) -> Any:
        from shared.services.retrieval.agent_tools import ToolResult

        self.dispatches.append(name)
        if name == "corpus.grep":
            page_index = self.discoveries
            self.discoveries += 1
            if page_index >= len(self.pages):
                raise AssertionError("runtime requested more than one correction")
            rows = [self.row(chunk_id) for chunk_id in self.pages[page_index]]
            self.last_refs = [
                {"document_id": self.document_id, "section_path": row["section_path"]}
                for row in rows
            ]
            discovery_rows = [
                {**ref, "chunk_id": row["chunk_id"], "mounted_chunk_ids": []}
                for ref, row in zip(self.last_refs, rows)
            ]
            return ToolResult(text=json.dumps(discovery_rows), payload={"rows": discovery_rows})
        if name != "corpus.read":
            raise AssertionError(f"unexpected fixture tool: {name}")
        rows = []
        statuses = []
        for ref in args["refs"]:
            address = (ref["document_id"], ref["section_path"])
            if address not in kwargs["readable"]:
                raise AssertionError("read bypassed the real discovery/pick protocol")
            chunk_id = ref["section_path"].rsplit(" / ", 1)[1]
            row = self.row(chunk_id)
            rows.append(row)
            statuses.append({**ref, "status": "ok", "chunk_ids": [chunk_id]})
        self.reads += 1
        self.pending_handles = [f"R{self.reads}.{i}" for i in range(1, len(rows) + 1)]
        return ToolResult(
            text="\n".join(row["content"] for row in rows),
            payload={"refs": statuses, "chunks": rows},
        )

    async def snapshot(self, *, pool: list[Any], queried_tables: Any) -> Any:
        from shared.services.retrieval.agent_explore.evidence_pool import compose_pool_evidence
        from shared.services.retrieval.execution.evidence_review import build_review_snapshot
        from shared.services.retrieval.hydration.evidence_compose import compose_evidence_parts

        ids = list(dict.fromkeys(
            chunk_id for candidate in pool for chunk_id in candidate.chunk_ids
        ))
        rows = [self.row(chunk_id) for chunk_id in ids]
        rows_by_id = {row["chunk_id"]: row for row in rows}
        for row in rows:
            row["composed"] = compose_evidence_parts(row, rows_by_id, queried_tables)
        evidence = compose_pool_evidence(pool, rows)
        self.snapshot_sources.append(ids)
        self.latest_snapshot = build_review_snapshot(
            query=self.query, pool=pool, rows=rows, evidence=evidence,
        )
        return self.latest_snapshot


class ScriptedExplorer:
    """Identical explorer policy in both arms; no gold or verdict inspection."""

    def __init__(self, corpus: FrozenCorpus) -> None:
        self.corpus = corpus
        self.phase = "discover"
        self.calls = 0

    def chat_completion_raw_with_usage(self, **kwargs: Any) -> tuple[Any, dict[str, int]]:
        self.calls += 1
        choice = kwargs["tool_choice"]
        forced = choice.get("function", {}).get("name") if isinstance(choice, dict) else None
        if forced == "corpus_pick":
            name, args = "corpus_pick", {"pick": self.corpus.pending_handles}
        elif forced == "finish":
            name, args = "finish", {"notes": FINISH_NOTE}
        elif self.phase == "discover":
            self.phase = "read"
            name, args = "corpus_grep", {"pattern": self.corpus.query}
        elif self.phase == "read" and self.corpus.last_refs:
            self.phase = "finish"
            name, args = "corpus_read", {"refs": self.corpus.last_refs}
        else:
            self.phase = "discover"
            name, args = "finish", {"notes": FINISH_NOTE}
        call = SimpleNamespace(
            id=f"scripted-{self.calls}",
            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        )
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(tool_calls=[call], content="")
        )])
        return response, {"total_tokens": EXPLORER_TOKENS}


class ScriptedReviewer:
    """Predetermined transport outputs; the real parser and policy run intact."""

    def __init__(self, case: dict[str, Any], corpus: FrozenCorpus) -> None:
        self.responses = case["scripted_reviews"]
        self.tokens_per_call = case.get("scripted_reviewer_tokens", REVIEWER_TOKENS)
        self.corpus = corpus
        self.calls = 0
        self.tokens = 0
        self.source_only_prompts: list[bool] = []

    async def __call__(
        self, messages: list[dict[str, Any]], max_tokens: int
    ) -> tuple[str, dict[str, int]]:
        message_text = "\n".join(str(message.get("content", "")) for message in messages)
        # User JSON escapes HTML quotes/newlines; parse it only for this audit,
        # without inspecting gold labels or choosing a verdict from its content.
        payload = json.loads(messages[1]["content"])
        expected_sources = {item.chunk_id: item.text for item in self.corpus.latest_snapshot.items}
        # v2 sends a lossless partition rather than a second copy of the text.
        # Reconstruct the whole received source solely for the input-isolation
        # audit; the fixture verdict and citations stay predetermined.
        received_sources = {item["chunk_id"]: "".join(span["text"] for span in item["citation_spans"])
                            for item in payload["evidence"]}
        source_only = (
            [message["role"] for message in messages] == ["system", "user"]
            and set(payload) == {"original_query", "frozen_requirements", "evidence"}
            and payload["original_query"] == self.corpus.query
            and received_sources == expected_sources
            and FINISH_NOTE not in message_text
        )
        for source in self.corpus.sources.values():
            if source.get("summary"):
                source_only &= source["summary"] not in message_text
        self.source_only_prompts.append(source_only)
        index = self.calls
        self.calls += 1
        if index >= len(self.responses):
            raise AssertionError("reviewer called beyond scripted response sequence")
        response = copy.deepcopy(self.responses[index])
        if "raise" in response:
            raise ConnectionError("controlled reviewer outage")
        # Fixture E1/E2 aliases index the frozen corpus. Resolve them to the
        # production snapshot's stable source IDs; never derive a verdict here.
        actual_ids = {item.chunk_id: item.evidence_id for item in self.corpus.latest_snapshot.items}
        aliases = {
            f"E{i + 1}": actual_ids.get(chunk_id, f"absent-{chunk_id}")
            for i, chunk_id in enumerate(self.corpus.sources)
        }
        for facet in response.get("coverage", []):
            for citation in facet["citations"]:
                citation["evidence_id"] = aliases[citation["evidence_id"]]
        self.tokens += self.tokens_per_call
        raw = response.get("raw", json.dumps(response))
        return raw, {"total_tokens": self.tokens_per_call}


def evaluate_gold(case: dict[str, Any], selected: set[str]) -> dict[str, Any]:
    groups = case["gold_requirements"]
    covered = sum(bool(selected.intersection(alternatives)) for alternatives in groups)
    return {
        "gold_evidence_coverage": covered / len(groups) if groups else None,
        "gold_sufficient": bool(case["answerable"] and groups and covered == len(groups)),
    }


async def run_arm(case: dict[str, Any], *, enabled: bool, mode: str) -> dict[str, Any]:
    from shared.services.retrieval.agent_explore.budget import EpisodeBudget
    from shared.services.retrieval.agent_explore.evidence_pool import compose_pool_evidence
    from shared.services.retrieval.agent_explore.evidence_review import EvidenceReviewSession
    from shared.services.retrieval.agent_explore.harness import openai_harness
    from shared.services.retrieval.execution.evidence_review import build_review_snapshot
    from shared.services.retrieval.hydration.evidence_compose import compose_evidence_parts

    corpus = FrozenCorpus(case)
    explorer = ScriptedExplorer(corpus)
    reviewer = ScriptedReviewer(case, corpus)
    review = EvidenceReviewSession(
        query=case["query"], load_snapshot=corpus.snapshot,
        reviewer=reviewer if mode == "scripted" else None,
    ) if enabled else None
    budget = EpisodeBudget(
        max_steps=case.get("max_steps", 15),
        wall_clock_seconds=case.get("wall_clock_seconds", 180),
    )
    started = time.perf_counter()
    with patch.object(openai_harness, "_resolve_client_and_model", return_value=(explorer, "scripted-explorer-v1")), patch.object(openai_harness, "dispatch_tool_call", corpus.dispatch):
        episode = await openai_harness.OpenAIHarness().run_episode(
            db_factory=lambda: None,
            user_id="fixture-user", namespace="evidence-review-experiment",
            query=case["query"], budget=budget, evidence_review=review,
        )
    duration_ms = (time.perf_counter() - started) * 1000
    selected_ids = list(dict.fromkeys(
        chunk_id for candidate in episode.pool for chunk_id in candidate.chunk_ids
    ))
    selected = set(selected_ids)
    rows = [corpus.row(chunk_id) for chunk_id in selected_ids]
    rows_by_id = {row["chunk_id"]: row for row in rows}
    for row in rows:
        row["composed"] = compose_evidence_parts(row, rows_by_id, episode.queried_tables)
    evidence = compose_pool_evidence(episode.pool, rows)
    if review:
        review.finalize(build_review_snapshot(
            query=case["query"], pool=episode.pool, rows=rows, evidence=evidence,
        ))
    report = review.report if review else {}
    evidence_text = "\n".join(part.get("text", "") for part in evidence)
    evidence_only = FINISH_NOTE not in evidence_text and not (
        report.get("reason") and report["reason"] in evidence_text
    )
    gold = evaluate_gold(case, selected)
    status = report.get("status", "disabled")
    reviewer_tokens = int(report.get("reviewer_tokens", 0))
    explorer_tokens = explorer.calls * EXPLORER_TOKENS
    calls = int(report.get("attempts", 0))
    result = {
        "variant": "review" if enabled else "baseline",
        "selected_chunk_ids": sorted(selected),
        **gold,
        "review_status": status,
        "false_sufficient": status == "sufficient" and not gold["gold_sufficient"],
        "answerable_finished_without_sufficient_gold": bool(
            case["answerable"] and not gold["gold_sufficient"]
            and episode.stop_reason == "finished"
        ),
        "nonanswerable_rejected": not case["answerable"] and status == "insufficient",
        "stop_reason": episode.stop_reason,
        "review_calls": calls,
        "corrective_retrievals": max(0, corpus.discoveries - 1),
        "steps_used": budget.steps_used,
        "initial_max_steps": case.get("max_steps", 15),
        "final_max_steps": budget.max_steps,
        "explorer_tokens": explorer_tokens,
        "reviewer_tokens": reviewer_tokens,
        "total_tokens": episode.tokens_used,
        "usage_complete": report.get("usage_complete", True),
        "duration_ms": round(duration_ms, 3),
        "finish_review_duration_ms": sum(
            step.elapsed_ms for step in episode.steps if step.tool_name == "evidence_review"
        ),
        "review_report": report,
        "trace_actions": [step.tool_name for step in episode.steps],
        "snapshot_chunk_ids": corpus.snapshot_sources,
        "gates": {
            "evidence_only": evidence_only,
            "at_most_one_correction": corpus.discoveries <= 2,
            "at_most_two_reviews": calls <= 2,
            "token_accounting": episode.tokens_used == explorer_tokens + reviewer_tokens,
            "disabled_has_no_review_calls": enabled or calls == reviewer.calls == 0,
            "disabled_has_no_snapshot_loads": enabled or not corpus.snapshot_sources,
            "enabled_total_step_cap": not enabled or budget.steps_used <= case.get("max_steps", 15),
        },
    }
    if mode == "scripted":
        result["gates"].update({
            "source_only_reviewer_input": all(reviewer.source_only_prompts),
            "review_call_accounting": calls == reviewer.calls,
            "review_token_accounting": reviewer_tokens == reviewer.tokens,
        })
        if enabled:
            result["gates"].update({
                "expected_review_calls": calls == case["expected_review_calls"],
                "expected_repairs": report.get("repairs", 0) == case["expected_repairs"],
                "expected_status": status == case["expected_status"],
                "false_sufficiency_negative_control": result["false_sufficient"] == case.get("expected_false_sufficient", False),
            })
    return result


def aggregate(trials: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for variant in ("baseline", "review"):
        arms = [trial[variant] for trial in trials]
        answerable = [trial[variant] for trial in trials if trial["answerable"]]
        coverage = [arm["gold_evidence_coverage"] for arm in answerable if arm["gold_evidence_coverage"] is not None]
        insufficient_packets = [arm for arm in arms if not arm["gold_sufficient"]]
        nonanswerable = [trial[variant] for trial in trials if not trial["answerable"]]
        sufficient = [arm for arm in arms if arm["review_status"] == "sufficient"]
        false_sufficient = sum(arm["false_sufficient"] for arm in arms)
        negative_controls = [trial[variant] for trial in trials if trial["negative_control"]]
        summary[variant] = {
            "runs": len(arms),
            "mean_answerable_gold_coverage": statistics.mean(coverage) if coverage else None,
            "answerable_gold_sufficient_runs": sum(arm["gold_sufficient"] for arm in answerable),
            "answerable_runs": len(answerable),
            "completed_without_sufficient_gold": len(insufficient_packets),
            "answerable_finished_without_sufficient_gold": sum(arm["answerable_finished_without_sufficient_gold"] for arm in answerable),
            "sufficient_claims": len(sufficient),
            "false_sufficient_claims": false_sufficient,
            "negative_control_runs": len(negative_controls),
            "negative_control_false_sufficient_claims": sum(arm["false_sufficient"] for arm in negative_controls),
            "false_sufficient_rate_on_insufficient_packets": false_sufficient / len(insufficient_packets) if variant == "review" and insufficient_packets else None,
            "false_sufficient_share_of_sufficient_claims": false_sufficient / len(sufficient) if sufficient else None,
            "nonanswerable_rejected": sum(arm["nonanswerable_rejected"] for arm in nonanswerable),
            "nonanswerable_runs": len(nonanswerable),
            "review_calls": sum(arm["review_calls"] for arm in arms),
            "total_tokens": sum(arm["total_tokens"] for arm in arms),
            "reviewer_tokens": sum(arm["reviewer_tokens"] for arm in arms),
            "unknown_usage_runs": sum(not arm["usage_complete"] for arm in arms),
            "mean_duration_ms": statistics.mean(arm["duration_ms"] for arm in arms),
            "failed_gates": sum(not passed for arm in arms for passed in arm["gates"].values()),
        }
    summary["paired_mean_coverage_delta"] = statistics.mean(
        trial["review"]["gold_evidence_coverage"] - trial["baseline"]["gold_evidence_coverage"]
        for trial in trials if trial["answerable"]
        and trial["baseline"]["gold_evidence_coverage"] is not None
    ) if any(trial["answerable"] for trial in trials) else None
    return summary


async def experiment(arguments: argparse.Namespace, fixture: dict[str, Any]) -> dict[str, Any]:
    cases = [case for case in fixture["cases"] if not arguments.case or case["case_id"] in arguments.case]
    if arguments.mode == "live-reviewer":
        cases = [case for case in cases if case["case_id"] not in LIVE_EXCLUDED]
        from shared.core.config import settings
        if settings.LLM_MOCK_ENABLED or not settings.DS_KEY:
            raise ValueError("live-reviewer requires configured DS_KEY and LLM_MOCK_ENABLED=false")
    if not cases:
        raise ValueError("no matching cases")
    trials = []
    for repeat in range(arguments.repeats):
        for index, case in enumerate(cases):
            # Alternate pair order to reduce warm-up/order bias in local timings.
            order = (False, True) if (repeat + index) % 2 == 0 else (True, False)
            trial: dict[str, Any] = {
                "case_id": case["case_id"], "repeat": repeat,
                "answerable": case["answerable"],
                "negative_control": case.get("negative_control", False),
                "order": ["review" if enabled else "baseline" for enabled in order],
            }
            for enabled in order:
                arm = await run_arm(case, enabled=enabled, mode=arguments.mode)
                trial[arm["variant"]] = arm
            trials.append(trial)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=False,
        capture_output=True, text=True,
    ).stdout.strip()
    source_paths = [
        ROOT / "packages/shared-python/shared/services/retrieval/agent_explore/evidence_review.py",
        ROOT / "packages/shared-python/shared/services/retrieval/agent_explore/harness/openai_harness.py",
        ROOT / "packages/shared-python/shared/services/retrieval/execution/evidence_review.py",
    ]
    return {
        "schema_version": "evidence-review-experiment/1",
        "mode": arguments.mode,
        "claim_scope": "Production finish-review integration; frozen synthetic corpus and scripted explorer. Scripted reviewer outputs and token usage are predetermined, not model-quality evidence. Live mode measures only the real reviewer, with a scripted explorer and corpus backend.",
        "token_interpretation": "All tokens simulated" if arguments.mode == "scripted" else "Reviewer tokens provider-reported; explorer tokens simulated",
        "fixture_digest": digest(fixture),
        "experiment_script_digest": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "git_revision": revision,
        "runtime_source_digests": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths},
        "python_version": platform.python_version(),
        "explorer_model": "scripted-explorer-v1",
        "reviewer_model": "scripted-reviewer-v1" if arguments.mode == "scripted" else "configured production reviewer",
        "repeats": arguments.repeats,
        "summary": aggregate(trials),
        "trials": trials,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("scripted", "live-reviewer"), default="scripted")
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--case", action="append", help="Run one case ID; repeat to select several")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path, help="Write complete JSON report; stdout stays compact")
    arguments = parser.parse_args()
    if arguments.repeats < 1:
        parser.error("--repeats must be positive")
    prepare_imports()
    fixture = json.loads(arguments.fixtures.read_text())
    try:
        report = asyncio.run(experiment(arguments, fixture))
    except Exception as exc:
        # Provider exception strings can contain endpoint or credential data.
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"mode": report["mode"], "summary": report["summary"], "output": str(arguments.output) if arguments.output else None}, indent=2))
    return int(any(arm["failed_gates"] for key, arm in report["summary"].items() if key in {"baseline", "review"}))


if __name__ == "__main__":
    raise SystemExit(main())
