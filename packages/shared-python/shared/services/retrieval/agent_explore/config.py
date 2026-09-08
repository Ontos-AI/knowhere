"""Production config for the ``agent_explore`` in-process tool-loop.

Deliberately independent from ``nav_config.py`` / ``nav/`` — see
``nav_config.py``'s "not read by agent_explore" note. This package must have
no runtime import from ``nav/`` so Phase 5 can delete that package once
``agent_explore`` passes its Phase 4 evaluation gate, without having to first
extract anything out of ``nav/`` for this package to keep working.

Model choice: reuses the same literal model name as ``nav_config.MAPNAV_MODEL``
(``deepseek-v4-flash``) — not by importing that module, but as its own
constant — because tool-calling (single, parallel, and forced ``tool_choice``)
was verified live against this exact model during Phase 3 design; no other
model has been verified for this codebase's OpenAI-compatible client.

``AGENT_EXPLORE_MAX_STEPS`` / ``AGENT_EXPLORE_WALL_CLOCK_SECONDS`` are new
product constants (not specified by the plan text, which only named the two
budget *dimensions* to add). Disclosed here rather than buried: revisit in
Phase 4 evaluation once real latency data exists.
"""

from __future__ import annotations

AGENT_EXPLORE_MODEL = "deepseek-v4-flash"

# Model for AGENT_EXPLORE_HARNESS=cursor_sdk (harness/cursor_harness.py) —
# a separate constant from AGENT_EXPLORE_MODEL because that harness's
# provider (Cursor SDK) is a disjoint model catalog from the OpenAI-compatible
# client's, not an interchangeable choice. "composer-2.5" is what the PoC
# (apps/worker/scripts/debug_cursor_agent_explore.py) verified live and
# noticeably outperformed deepseek-v4-flash on the two hardest eval-fixture
# queries (q04/q06) — see the Phase 3.5 landing record.
AGENT_EXPLORE_CURSOR_MODEL = "composer-2.5"

# One LLM turn = one round-trip that may contain several parallel tool calls
# (see episode.py). Kept low relative to map-nav's per-node dispatch depth
# (≤5) because each turn here can already resolve several tools at once.
AGENT_EXPLORE_MAX_STEPS = 12

# Wall-clock ceiling for the whole episode (LLM round-trips + tool
# dispatch), independent of the token budget. New constant, not tuned yet.
AGENT_EXPLORE_WALL_CLOCK_SECONDS = 180.0

# Max tokens requested per LLM completion turn (thinking disabled — see
# episode.py; this is completion budget, not context window).
AGENT_EXPLORE_MAX_COMPLETION_TOKENS = 1024

FINISH_TOOL_NAME = "finish"

FINISH_TOOL_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "refs": {
            "type": "array",
            "description": (
                "Final cited evidence, in priority order. Each item "
                "identifies one section or chunk you have already looked at "
                "via corpus.read (or, for an asset, corpus.assets)."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "section_path": {"type": "string"},
                    "chunk_id": {"type": "string"},
                },
                "required": ["document_id"],
            },
        },
        "notes": {
            "type": "string",
            "description": (
                "Optional short note on why these refs answer the query, "
                "or why none were found."
            ),
        },
    },
    "required": ["refs"],
}

FINISH_TOOL_DESCRIPTION = (
    "Call this when you have gathered enough evidence to answer the query, "
    "or when you are certain the corpus does not contain an answer. This "
    "ends the exploration — do not answer in plain text; the final answer "
    "is synthesized downstream from the refs you cite here."
)

# Appended after the verbatim CORPUS_SCHEMA.md text (schema_doc.py) to form
# this harness's system prompt. Kept out of CORPUS_SCHEMA.md itself because
# the finish-tool loop contract is agent_explore-specific, not something the
# MCP-facing harnesses (Cursor/Codex/Claude) need — see that file's own
# "single source... do not duplicate" header.
LOOP_CONTRACT_SUFFIX = f"""

---

## Exploration loop contract

You are exploring this corpus autonomously to answer one query. Use the
tools above to navigate; you may call several tools in one turn when they
are independent. When you have enough evidence, call `{FINISH_TOOL_NAME}`
with the `refs` you want cited as the answer — do not write the final answer
as plain text yourself, it is synthesized downstream from your cited refs.
If you exhaust your tool budget without a confident answer, call
`{FINISH_TOOL_NAME}` with your best-effort `refs` (or an empty list plus a
`notes` explanation of why nothing was found) rather than continuing to
call other tools.

`refs` is REQUIRED and must not be omitted or left empty if you called
`corpus.read` (or `corpus.assets`) even once during this exploration: copy
the `document_id` and `chunk_id`/`section_path` of every section/chunk you
read that supports your answer into `refs` before calling
`{FINISH_TOOL_NAME}`. Calling `{FINISH_TOOL_NAME}` with no `refs` after
having already read relevant content discards that evidence.
"""
