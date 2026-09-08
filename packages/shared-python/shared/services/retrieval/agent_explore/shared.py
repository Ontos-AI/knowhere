"""Provider-agnostic helpers shared by every ``agent_explore`` harness.

Extracted from ``episode.py`` (originally OpenAI-harness-specific) once a
second harness (``harness/cursor_harness.py``) needed the exact same logic
and, as a debug-script PoC, had started duplicating and drifting from it
instead of sharing it — see the Phase 3.5 section of
``.cursor/plans/agentic_corpus_explore_retrieval_c2c4ea21.plan.md``.

Deliberately excludes anything that assumes an editable ``messages:
list[dict]`` conversation history (that's OpenAI-harness-specific — see
``harness/openai_harness.py``'s ``_collapse_stale_tool_messages``, which is
NOT here because the Cursor SDK manages its own context with no equivalent
hook exposed to the host process).
"""

from __future__ import annotations

from typing import Any

from shared.services.retrieval.agent_explore.budget import EpisodeBudget
from shared.services.retrieval.agent_tools import ToolResult

# Tools whose ToolResult.refs point at evidence the agent has actually looked
# at (full body content), as opposed to candidate/listing refs from
# list_documents/outline/node_filter/recall/grep — those describe *where
# things are*, not *what was read*, and would inject unread noise into the
# trajectory-refs fallback below if included.
EVIDENCE_TOOL_NAMES = frozenset({"corpus.read", "corpus.assets"})


def wire_safe_tool_name(name: str) -> str:
    """Replace ``.`` with ``_`` in a tool name for function-calling wire formats.

    Every ``agent_tools`` name is dotted (``corpus.read``); DeepSeek's
    (OpenAI-compatible) function-calling API rejects ``.`` in
    ``tools[].function.name`` (must match ``^[a-zA-Z0-9_-]+$``, verified
    live), and the Cursor SDK PoC needed the identical replacement for its
    own ``custom_tools`` wire names — this is a function-calling wire-format
    restriction shared by both providers, not an OpenAI-specific quirk. The
    dotted name stays canonical in ``REGISTRY``/MCP; this underscore form
    exists only for providers whose wire format rejects dots.
    """
    return name.replace(".", "_")


def build_wire_tool_name_map(names: list[str]) -> dict[str, str]:
    """``{wire_safe_name: canonical_name}`` for every name in ``names``.

    Names that are already wire-safe (e.g. ``finish``, which has no dot) map
    to themselves. Used by a harness to translate a provider's tool-call
    name back to the canonical ``REGISTRY`` name before dispatch.
    """
    return {wire_safe_tool_name(name): name for name in names}


def tool_message_content(result: ToolResult, *, max_chars: int) -> str:
    """Cap a tool's rendered text before it enters LLM context.

    Uses the caller's ``ToolBudget.max_chars`` (``EVIDENCE_TEXT_CHAR_BUDGET``,
    aligned with evidence packing — see ``agent_tools/registry.py``)
    so tools like ``read`` can return unbounded body text while the harness
    still bounds what the model sees per turn. This cap applies uniformly to
    every tool's rendered text (not just ``read``'s body content) — a tool
    that returns a "complete, non-truncated" *matched set* by contract
    (``outline``, ``node_filter``) still has its *rendered text* capped here
    the same as any other tool; that promise is about payload/refs
    cardinality, not about how much of it is shown to the LLM per turn.
    """
    if result.error:
        return f"error: {result.error}"
    text = result.text or "(empty result)"
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return (
        text[:max_chars]
        + f"\n...[truncated, {omitted} more chars — narrow the scope "
        "(e.g. depth/path_prefix for outline, a tighter predicate for "
        "node_filter, or a more specific ref for read) and call again if "
        "you need the rest]"
    )


def normalize_finish_refs(raw: Any) -> list[dict[str, Any]]:
    """Keep only dict items with a non-empty ``document_id`` from a raw ``finish.refs``."""
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict) and str(item.get("document_id") or "").strip():
            normalized.append(item)
    return normalized


def budget_status_line(budget: EpisodeBudget) -> str:
    """One-line remaining-budget summary appended to a tool observation.

    Neither harness previously surfaced ``EpisodeBudget``'s own counters
    (``steps_used``/``max_steps``, ``tokens_used``/``token_limit``,
    elapsed/wall_clock) to the model at all — it had no way to tell "I'm on
    step 3 of 12" from "I'm on step 11 of 12", so it could not self-regulate
    when to stop exploring and call ``finish``. This exposes the same
    ``EpisodeBudget.snapshot()`` data already used for the hard cutoff,
    reused as a soft signal the model can read every turn.
    """
    snap = budget.snapshot()
    return (
        f"[budget: steps {snap['steps_used']}/{snap['max_steps']}, "
        f"tokens {snap['tokens_used']}/{snap['token_limit']}, "
        f"elapsed {snap['elapsed_seconds']:.0f}s/{snap['wall_clock_seconds']:.0f}s]"
    )


def dedup_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedup by ``(document_id, chunk_id)``, keeping first-seen order."""
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for ref in refs:
        document_id = str(ref.get("document_id") or "").strip()
        chunk_id = str(ref.get("chunk_id") or "").strip()
        key = (document_id, chunk_id)
        if not document_id or not chunk_id or key in seen:
            continue
        seen.add(key)
        deduped.append(ref)
    return deduped
