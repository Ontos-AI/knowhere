"""Agent-explore system prompt.

Tool-specific usage stays on each tool's own description. This module only
holds the shared overview plus the exploration-loop contract.
"""

from __future__ import annotations

from shared.services.retrieval.agent_tools import load_corpus_overview_text

LOOP_RULES = """\
## How this exploration works

You collect evidence for one user question; you do not write the answer.
Each turn, either call tools or call finish.

Reading:
- corpus.read only accepts addresses (document_id plus section_path or
  chunk_id) that appeared in results from your earlier turns. On your first
  turn you have no results yet, so do not call corpus.read.
- Calls in the same turn cannot use each other's results.

Evidence pool: only what you pick reaches the answer writer.
- A corpus.read result gives each successfully read ref a pick id (R3.1,
  R3.2, ...). A corpus.outline result gives the whole outline one pick id (O2).
  Failed refs get no id. Other tools give no ids: to pick an image or table,
  read it by chunk_id first.
- After a turn where corpus.read or corpus.outline succeeds, the next turn is
  a pick phase: you can only call corpus.pick. Pass the ids worth keeping, or
  an empty list. Picked ids join the evidence pool.
- Body text you read but did not pick cannot be read again.
- Each turn you see: a trace of your past calls (tool, arguments, ok or why
  it failed), the full results of your latest calls, the evidence pool, and
  the steps used so far.

Call finish when the pool is enough to answer, or when you are sure the
corpus has no answer. finish ends the exploration at once.

If a call needs another call's result, wait for that result instead of
issuing both in the same turn.

Tool names on the wire use "_" instead of "." (corpus_read = corpus.read).
"""

AGENT_SYSTEM_PROMPT = load_corpus_overview_text() + "\n\n" + LOOP_RULES
