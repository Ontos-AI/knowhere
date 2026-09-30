"""Agent-explore system prompt and pick-field wrapper.

Tool-specific usage stays on each tool's own description. This module only
holds the shared overview plus the exploration-loop contract.
"""

from __future__ import annotations

import copy
from typing import Any

from shared.services.retrieval.agent_tools import load_corpus_overview_text

LOOP_RULES = """\
## How this exploration works

You collect evidence for one user question; you do not write the answer.
Each turn, either call tools or call finish.

Evidence pool: only what you pick reaches the answer writer.
- A corpus.read result gives each successfully read ref a pick id (R3.1,
  R3.2, ...). A corpus.outline result gives the whole outline one pick id (O2).
  Failed refs get no id. Other tools give no ids: to pick an image or table,
  read it by chunk_id first.
- Pick ids are valid only in your very next action. Put the ones you want in
  that action's "pick" field (any tool call, or finish). Omit "pick" to take
  none. After that action the ids expire; to pick later, read again.
- Each turn you see: a trace of your past calls (tool, arguments, ok or why
  it failed), the full results of your latest calls, the evidence pool, and
  the remaining budget.

Call finish when the pool is enough to answer, or when you are sure the
corpus has no answer. finish ends the exploration at once; it may carry a
final "pick".

If a call needs another call's result, wait for that result instead of
issuing both in the same turn.

Tool names on the wire use "_" instead of "." (corpus_read = corpus.read).
"""

AGENT_SYSTEM_PROMPT = load_corpus_overview_text() + "\n\n" + LOOP_RULES

PICK_FIELD_SCHEMA: dict[str, object] = {
    "type": "array",
    "items": {"type": "string", "pattern": r"^(R\d+\.\d+|O\d+)$"},
    "uniqueItems": True,
    "description": (
        "Pick ids from the previous result to add to the evidence pool. "
        "Only ids shown with your latest results are accepted. "
        "Omit to pick nothing."
    ),
}


def with_pick_field(schema: dict[str, Any]) -> dict[str, Any]:
    wrapped = copy.deepcopy(schema)
    properties = dict(wrapped.get("properties") or {})
    properties["pick"] = PICK_FIELD_SCHEMA
    wrapped["properties"] = properties
    return wrapped


def split_pick(args: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    rest = dict(args)
    raw = rest.pop("pick", None)
    handles = [str(item).strip() for item in raw] if isinstance(raw, list) else []
    return [item for item in handles if item], rest
