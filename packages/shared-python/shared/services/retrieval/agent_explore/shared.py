"""Provider-agnostic helpers shared by every ``agent_explore`` harness.

Extracted from ``episode.py`` (originally OpenAI-harness-specific) once a
second harness (``harness/cursor_harness.py``) needed the exact same logic
and, as a debug-script PoC, had started duplicating and drifting from it
instead of sharing it — see the Phase 3.5 section of
``.cursor/plans/agentic_corpus_explore_retrieval_c2c4ea21.plan.md``.
"""

from __future__ import annotations

from typing import Any

import jsonschema
import jsonschema.validators

from shared.services.retrieval.agent_explore.config import (
    FINISH_TOOL_SCHEMA,
    PICK_TOOL_NAME,
    PICK_TOOL_SCHEMA,
)
from shared.services.retrieval.agent_tools import ToolResult

# The two map-narrowing tools bound their own text at MAP_TOOL_CHAR_BUDGET by
# folding whole subtrees (agent_tools.map_render) or failing the call, so
# tool_message_content never cuts them mid-row.
MAP_TOOL_NAMES = frozenset({"corpus.outline", "corpus.node_filter"})


def model_accepts_images(model: str) -> bool:
    """OpenAI-compatible models attach images only when the name contains vision."""
    return "vision" in str(model or "").lower()


def https_image_parts(result: ToolResult) -> list[dict[str, Any]]:
    """HTTPS image blocks for a vision harness. ``filesystem://`` is omitted."""
    parts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in result.media:
        url = str(item.get("url") or "").strip()
        if item.get("type") != "image_url" or not url.startswith("https://"):
            continue
        if url in seen:
            continue
        seen.add(url)
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def cursor_execute_content(
    result: ToolResult,
    *,
    text: str,
) -> str | list[dict[str, Any]]:
    """Cursor ``execute`` returns text, or text plus HTTPS image parts."""
    images = https_image_parts(result)
    if not images:
        return text
    return [{"type": "text", "text": text}, *images]


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


def tool_message_content(result: ToolResult, *, tool_name: str, max_chars: int) -> str:
    """Cap a tool's rendered text before it enters LLM context.

    Uses the caller's ``ToolBudget.max_chars`` (``EVIDENCE_TEXT_CHAR_BUDGET``,
    aligned with evidence packing — see ``agent_tools/registry.py``) so tools
    like ``read`` can return unbounded body text while the harness still
    bounds what the model sees per turn. ``MAP_TOOL_NAMES`` are returned
    whole: they already fit ``MAP_TOOL_CHAR_BUDGET`` or failed the call.
    """
    if result.error:
        return f"error: {result.error}"
    text = result.text or "(empty result)"
    if tool_name in MAP_TOOL_NAMES or len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    # TODO: return a fragment for oversized corpus.read bodies, the way oversized tables do, instead of this head cut.
    return (
        text[:max_chars]
        + f"\n...[truncated, {omitted} more chars — narrow the scope "
        "or use a more specific ref and call again if you need the rest]"
    )


def validate_finish_args(args: dict[str, Any]) -> str | None:
    """Validate ``finish``'s own args against ``FINISH_TOOL_SCHEMA``; ``None`` = valid.

    Both harnesses route ``finish`` argument validation through this one
    function instead of relying solely on the provider enforcing the closed
    schema at generation time — whether either provider actually does that
    is unverified (see ``config.py``'s ``FINISH_TOOL_SCHEMA`` docstring
    context), so this is the real enforcement point.
    """
    return _schema_args_error("finish", FINISH_TOOL_SCHEMA, args)


def validate_pick_args(args: dict[str, Any]) -> str | None:
    """Validate ``corpus.pick`` args against ``PICK_TOOL_SCHEMA``; ``None`` = valid.

    The Cursor SDK does not enforce ``input_schema`` (a live run sent
    ``pick_ids``), so a malformed call must fail here instead of being
    applied as an empty pick that closes the pick phase.
    """
    return _schema_args_error(PICK_TOOL_NAME, PICK_TOOL_SCHEMA, args)


def _schema_args_error(
    tool_name: str, schema: dict[str, object], args: dict[str, Any]
) -> str | None:
    if not isinstance(args, dict):
        return f"{tool_name}: arguments must be a JSON object, got {type(args).__name__}"
    validator_cls = jsonschema.validators.validator_for(schema)
    validator = validator_cls(schema)
    errors = sorted(
        validator.iter_errors(args), key=lambda error: [str(p) for p in error.path]
    )
    if not errors:
        return None
    error = errors[0]
    message = error.message
    if error.validator == "oneOf" and isinstance(error.schema, dict):
        message = str(error.schema.get("description") or message)
    location = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in error.path)
    return f"{tool_name}: invalid arguments{f' at {location}' if location else ''}: {message}"


def read_ref_status(tool_name: str, result: ToolResult) -> list[dict[str, Any]] | None:
    """``corpus.read``'s per-ref ok/failed list for ``AgentStep.ref_status``."""
    if tool_name != "corpus.read":
        return None
    statuses = result.payload.get("refs")
    return list(statuses) if isinstance(statuses, list) else None


def invalid_finish_message(error: str) -> str:
    """Model-facing text for a rejected ``finish`` call; the episode goes on."""
    return (
        f"{error}. finish was not accepted and the episode continues. Correct "
        'form: {"notes": "..."} — Call finish again '
        "with fixed arguments, or keep exploring with the corpus tools."
    )


def invalid_pick_message(error: str) -> str:
    """Model-facing text for a rejected ``corpus.pick`` call; the pick phase goes on."""
    return (
        f"{error}. Nothing was picked and the pick phase continues. Correct "
        'form: {"pick": ["<id>", ...]} or {"pick": []} — call corpus_pick again.'
    )
