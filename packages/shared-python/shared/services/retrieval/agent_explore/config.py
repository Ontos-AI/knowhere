"""Production config for the ``agent_explore`` in-process tool-loop.

Model choice for the OpenAI-compatible harness is ``deepseek-v4-flash``:
tool-calling (single, parallel, and forced ``tool_choice``) was verified
live against this exact model; no other model has been verified for this
codebase's OpenAI-compatible client.
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
# (see episode.py). Both harnesses count one explore turn as one step.
AGENT_EXPLORE_MAX_STEPS = 15

# Wall-clock ceiling for the whole episode (LLM round-trips + tool
# dispatch). A service-level guard, never shown to the model.
AGENT_EXPLORE_WALL_CLOCK_SECONDS = 180.0

# Max tokens requested per LLM completion turn (thinking disabled — see
# episode.py; this is completion budget, not context window).
AGENT_EXPLORE_MAX_COMPLETION_TOKENS = 1024

FINISH_TOOL_NAME = "finish"

FINISH_TOOL_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "string",
            "description": (
                "Optional short note for the answer writer, or why nothing "
                "was found."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
}

FINISH_TOOL_DESCRIPTION = (
    "End the exploration now. The evidence pool becomes the final evidence."
)

# Harness-level tool, not in agent_tools.REGISTRY, so MCP never exposes it.
PICK_TOOL_NAME = "corpus.pick"

PICK_TOOL_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "pick": {
            "type": "array",
            "items": {"type": "string", "pattern": r"^(R\d+\.\d+|O\d+)$"},
            "uniqueItems": True,
            "description": (
                "Ids from your latest corpus.read / corpus.outline results to "
                "keep. Pass an empty list to keep none."
            ),
        },
    },
    "required": ["pick"],
    "additionalProperties": False,
}

PICK_TOOL_DESCRIPTION = (
    "Pick which of your latest corpus.read / corpus.outline results join the "
    "evidence pool. Body text you read but do not pick cannot be read again."
)
