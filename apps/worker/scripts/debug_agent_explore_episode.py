"""Audit-only: dump every step of one ``agent_explore`` episode.

Prints, for each LLM turn / tool call: tool name, args, elapsed ms,
tokens_used_delta (turn-level, attributed to the first tool step — see
``episode.py``), tokens_used_total (cumulative), observation length (chars
sent back into the LLM's context), and error. Also prints the final
``EpisodeResult`` (refs/notes/stop_reason).

Read-only diagnostic; does not modify any behavior.

Usage:
  cd apps/worker
  uv run python scripts/debug_agent_explore_episode.py --query-id q04
  uv run python scripts/debug_agent_explore_episode.py --query "..." --token-limit 200000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]
sys.path.insert(0, str(_REPO_ROOT / "packages" / "shared-python"))
sys.path.insert(0, str(_SCRIPT_DIR.parent))

load_dotenv(_SCRIPT_DIR.parent / ".env")
os.environ.setdefault("LOCAL_DEBUG", "0")
os.environ.setdefault("LLM_MOCK_ENABLED", "false")

FIXTURE_PATH = _SCRIPT_DIR / "fixtures" / "changheba_archive_eval_queries.json"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query-id", default=None)
    parser.add_argument("--query", default=None)
    parser.add_argument("--user-id", default="debug_local_user")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--token-limit", type=int, default=None)
    args = parser.parse_args()

    query = args.query
    if args.query_id:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        match = next(q for q in fixture["queries"] if q["id"] == args.query_id)
        query = match["query"]
    if not query:
        raise SystemExit("need --query or --query-id")

    from shared.core.database import get_db_context
    from shared.services.retrieval.agent_explore.budget import EpisodeBudget
    from shared.services.retrieval.agent_explore.episode import run_agent_explore_episode

    budget = EpisodeBudget(token_limit=args.token_limit) if args.token_limit else None

    print(f"query: {query!r}")
    async with get_db_context() as db:
        episode = await run_agent_explore_episode(
            db=db,
            user_id=args.user_id,
            namespace=args.namespace,
            query=query,
            budget=budget,
        )

    print(f"\nstop_reason={episode.stop_reason} tokens_used={episode.tokens_used} "
          f"model={episode.model_name}")
    print(f"final refs ({len(episode.refs)}): {json.dumps(episode.refs, ensure_ascii=False)}")
    print(f"final notes: {episode.notes!r}")
    print(f"\n{'#':>3} {'tool':<28} {'ms':>6} {'delta':>7} {'total':>7} {'obs_chars':>9} err")
    for step in episode.steps:
        args_preview = json.dumps(step.tool_args, ensure_ascii=False)[:80]
        err = step.error or ""
        print(
            f"{step.step_index:>3} {step.tool_name or '(none)':<28} "
            f"{step.elapsed_ms:>6} {step.tokens_used_delta:>7} "
            f"{step.tokens_used_total:>7} {len(step.observation_text):>9} {err}"
        )
        print(f"      args: {args_preview}")


if __name__ == "__main__":
    asyncio.run(main())
