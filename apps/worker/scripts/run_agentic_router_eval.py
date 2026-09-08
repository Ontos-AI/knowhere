"""Smoke-run agent_explore on a fixed query set.

Reads ``fixtures/changheba_archive_eval_queries.json`` (sourced from
``zh_档案知识库测试样例.docx``) and runs each query through
``run_retrieval_route`` directly, bypassing the Redis result cache.

Usage:
  cd apps/worker
  uv run python scripts/run_agentic_router_eval.py
  uv run python scripts/run_agentic_router_eval.py --query-id q05
  uv run python scripts/run_agentic_router_eval.py --use-agentic false
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]
_SHARED_PYTHON = _REPO_ROOT / "packages" / "shared-python"
sys.path.insert(0, str(_SHARED_PYTHON))
sys.path.insert(0, str(_SCRIPT_DIR.parent))

load_dotenv(_SCRIPT_DIR.parent / ".env", override=True)
os.environ.setdefault("LOCAL_DEBUG", "0")
os.environ.setdefault("LLM_MOCK_ENABLED", "false")

FIXTURE_PATH = _SCRIPT_DIR / "fixtures" / "changheba_archive_eval_queries.json"
_MODES = ("classic", "agent_explore")
TOP_K = 10


def _count_llm_steps(decision_trace: list[dict[str, Any]], mode: str) -> int:
    del mode
    return sum(
        1
        for step in decision_trace
        if step.get("phase") in ("tool_call", "finish")
        or (step.get("decision") or {}).get("action") not in ("", "no_tool_call", None)
    )


def _keyword_hits(evidence_text: str, keywords: list[str]) -> tuple[int, list[str]]:
    text = evidence_text or ""
    hits = [kw for kw in keywords if kw and kw in text]
    return len(hits), hits


@dataclass
class RunMetrics:
    query_id: str
    router: str  # "classic" | "agent_explore"
    total_ms: int
    router_used: str
    stop_reason: str
    refs: int
    results: int
    evidence_chars: int
    llm_steps: int
    keyword_hits: int
    keyword_total: int
    matched_keywords: list[str]
    error: str | None = None


async def _run_one(
    *,
    user_id: str,
    namespace: str,
    query: str,
    query_id: str,
    router: str,
    use_agentic: bool,
    expected_keywords: list[str],
) -> RunMetrics:
    from dataclasses import replace

    from shared.core.database import get_db_context
    from shared.services.retrieval.execution.plan import project_public_retrieval_response
    from shared.services.retrieval.execution.query_request import RetrievalQuery
    from shared.services.retrieval.execution.revision_pins import (
        capture_revision_pins,
        is_revision_generation_stable,
    )
    from shared.services.retrieval.execution.routes import run_retrieval_route
    from shared.services.retrieval.settings import INTERNAL_RECALL_K_MULTIPLIER

    started = time.monotonic()
    error: str | None = None
    response: dict[str, Any] = {}
    try:
        async with get_db_context() as db:
            request = RetrievalQuery.from_parameters(
                db=db,
                user_id=user_id,
                namespace=namespace,
                query=query,
                top_k=TOP_K,
                exclude_document_ids=[],
                exclude_sections=[],
                use_agentic=use_agentic,
            )
            revision_pins = await capture_revision_pins(
                db, user_id=user_id, namespace=namespace
            )
            if not await is_revision_generation_stable(
                db,
                user_id=user_id,
                namespace=namespace,
                pins=revision_pins,
            ):
                revision_pins = await capture_revision_pins(
                    db, user_id=user_id, namespace=namespace
                )
            effective_recall_k = (
                request.internal_recall_k
                if request.internal_recall_k is not None
                else TOP_K * INTERNAL_RECALL_K_MULTIPLIER
            )
            context = replace(
                request.build_route_context(),
                revision_pins=revision_pins,
                effective_recall_k=effective_recall_k,
            )
            outcome = await run_retrieval_route(context)
            response = await project_public_retrieval_response(outcome.response)
    except Exception as exc:  # noqa: BLE001 - eval runner must continue
        error = f"{type(exc).__name__}: {exc}"

    elapsed_ms = int((time.monotonic() - started) * 1000)
    evidence = str(response.get("evidence_text") or "")
    decision_trace = response.get("decision_trace") or []
    hits, matched = _keyword_hits(evidence, expected_keywords)
    return RunMetrics(
        query_id=query_id,
        router=router,
        total_ms=elapsed_ms,
        router_used=str(response.get("router_used") or router),
        stop_reason=str(response.get("stop_reason") or ""),
        refs=len(response.get("referenced_chunks") or []),
        results=len(response.get("results") or []),
        evidence_chars=len(evidence),
        llm_steps=_count_llm_steps(decision_trace, router),
        keyword_hits=hits,
        keyword_total=len(expected_keywords),
        matched_keywords=matched,
        error=error,
    )


def _render_markdown(
    fixture: dict[str, Any],
    runs: list[RunMetrics],
    output_dir: Path,
) -> str:
    by_key = {(r.query_id, r.router): r for r in runs}
    lines = [
        "# Agentic Router Eval (Phase 4)\n",
        f"Fixture: `{FIXTURE_PATH.name}`\n",
        f"Corpus: `{fixture['corpus']['namespace']}` / "
        f"{len(fixture['corpus']['documents'])} documents\n",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}\n",
        f"Output dir: `{output_dir}`\n",
        "\n## Summary\n",
        "| Q | Query (short) | Router | ms | LLM steps | refs | kw hit | stop |\n",
        "|---|---|---|---:|---:|---:|---:|---|\n",
    ]
    for item in fixture["queries"]:
        qid = item["id"]
        short = item["query"][:28] + ("…" if len(item["query"]) > 28 else "")
        for router in _MODES:
            r = by_key.get((qid, router))
            if r is None:
                continue
            kw = f"{r.keyword_hits}/{r.keyword_total}"
            stop = (r.stop_reason or r.error or "")[:24]
            lines.append(
                f"| {qid} | {short} | {router} | {r.total_ms} | "
                f"{r.llm_steps} | {r.refs} | {kw} | {stop} |\n"
            )

    classic_ms = [r.total_ms for r in runs if r.router == "classic" and not r.error]
    agent_ms = [r.total_ms for r in runs if r.router == "agent_explore" and not r.error]

    def _p50(values: list[int]) -> int | None:
        if not values:
            return None
        ordered = sorted(values)
        return ordered[len(ordered) // 2]

    lines.extend(
        [
            "\n## Aggregate latency (successful runs only)\n",
            f"- classic p50: {_p50(classic_ms)} ms ({len(classic_ms)} runs)\n",
            f"- agent_explore p50: {_p50(agent_ms)} ms ({len(agent_ms)} runs)\n",
            "\n## Notes\n",
            "- Runs bypass Redis retrieval cache (direct ``run_retrieval_route``).\n",
            "- ``kw hit`` = expected keywords found in ``evidence_text`` " +
            "(proxy for recall quality, not a full answer judge).\n",
            "- Phase 0 ad-hoc queries are separate; this set is the 8 questions " +
            "from ``zh_档案知识库测试样例.docx``.\n",
        ]
    )
    return "".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-run agent_explore eval fixture")
    parser.add_argument(
        "--fixture",
        default=str(FIXTURE_PATH),
        help="Path to eval queries JSON",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default=None,
        help="Output directory (default: /tmp/agentic_router_eval/<timestamp>)",
    )
    parser.add_argument(
        "--query-id",
        action="append",
        default=[],
        help="Run only these query ids (repeatable, e.g. --query-id q01)",
    )
    parser.add_argument(
        "--use-agentic",
        choices=["true", "false", "both"],
        default="both",
        help="classic (false), agent_explore (true), or both",
    )
    args = parser.parse_args()

    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    queries = fixture["queries"]
    if args.query_id:
        allowed = set(args.query_id)
        queries = [q for q in queries if q["id"] in allowed]

    if args.use_agentic == "true":
        routers = ["agent_explore"]
    elif args.use_agentic == "false":
        routers = ["classic"]
    else:
        routers = list(_MODES)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir or f"/tmp/agentic_router_eval/{timestamp}")
    output_dir.mkdir(parents=True, exist_ok=True)

    user_id = fixture["corpus"]["user_id"]
    namespace = fixture["corpus"]["namespace"]

    all_runs: list[RunMetrics] = []
    for item in queries:
        for router in routers:
            label = f"{item['id']}_{router}"
            print(f"▶ {label}: {item['query'][:60]}…", flush=True)
            metrics = await _run_one(
                user_id=user_id,
                namespace=namespace,
                query=item["query"],
                query_id=item["id"],
                router=router,
                use_agentic=router != "classic",
                expected_keywords=item.get("expected_keywords") or [],
            )
            all_runs.append(metrics)
            print(
                f"  done {metrics.total_ms}ms refs={metrics.refs} "
                f"kw={metrics.keyword_hits}/{metrics.keyword_total} "
                f"stop={metrics.stop_reason or metrics.error}",
                flush=True,
            )

    report = {
        "fixture": args.fixture,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "routers": routers,
        "runs": [r.__dict__ for r in all_runs],
    }
    json_path = output_dir / "eval_report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    md = _render_markdown(fixture, all_runs, output_dir)
    md_path = output_dir / "eval_report.md"
    md_path.write_text(md, encoding="utf-8")

    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    print("\n" + md)


if __name__ == "__main__":
    asyncio.run(main())
