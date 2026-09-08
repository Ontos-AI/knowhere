"""Audit-only: monkeypatch to see the RAW ``finish`` tool_call.function.arguments
string the LLM actually sent, before ``_safe_json_loads`` parses it. Does not
modify any file — patches the imported module object in this process only.
"""

from __future__ import annotations

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
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--query-id", required=True)
    args = parser.parse_args()

    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    match = next(q for q in fixture["queries"] if q["id"] == args.query_id)
    query = match["query"]

    from shared.services.retrieval.agent_explore.harness import openai_harness

    original_safe_json_loads = openai_harness._safe_json_loads

    def _spy(raw):
        print(f"[SPY] raw arguments received: {raw!r}")
        return original_safe_json_loads(raw)

    openai_harness._safe_json_loads = _spy

    from shared.core.database import get_db_context
    from shared.services.retrieval.agent_explore.budget import EpisodeBudget

    print(f"query: {query!r}")
    result = await openai_harness.OpenAIHarness().run_episode(
        db_factory=get_db_context,
        user_id="debug_local_user",
        namespace="default",
        query=query,
        budget=EpisodeBudget(),
    )
    print(f"\nfinal refs: {result.refs}")
    print(f"final notes: {result.notes!r}")
    print(f"stop_reason: {result.stop_reason}")


if __name__ == "__main__":
    asyncio.run(main())
