"""Production config for the ``agent_explore`` in-process tool-loop.

Model choice for the OpenAI-compatible harness is ``deepseek-v4-flash``:
tool-calling (single, parallel, and forced ``tool_choice``) was verified
live against this exact model; no other model has been verified for this
codebase's OpenAI-compatible client.
"""

from __future__ import annotations

from shared.services.retrieval.agent_explore.prompt import PICK_FIELD_SCHEMA

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
# (see episode.py).
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
        "pick": PICK_FIELD_SCHEMA,
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
    "End the exploration now. The evidence pool becomes the final evidence. "
    "Use pick to add ids from the previous result before ending."
)
