"""Prompt templates and response parsers for agentic retrieval."""
from __future__ import annotations

import json
import re
from typing import Any


FILE_SELECT_PROMPT = """\
You are a document routing assistant.

{budget_block}
Below is a document corpus overview showing all available documents,
their navigation summaries, chunk counts, and media counts.
Some documents may show "🔍 Discovery hints" — these are preliminary keyword
matches from bottom-up search. Consider them as additional signals but make
your own judgment on document relevance.

=== Document Corpus Overview ===
{overview}
=== End Overview ===

User query: {query}
Based on the query, select documents that may contain relevant information.
If NO document in the corpus is relevant to the query, return an EMPTY array [].
Return ONLY a JSON array of document IDs, e.g.: ["doc_abc123", "doc_def456"]
Do not include any explanation.
"""


DISCOVERY_SELECT_PROMPT = """\
You are a document navigation assistant.

Document: "{doc_name}"

{budget_block}
After navigating the document's section tree, the following section paths
were additionally discovered via keyword and semantic search.
They may contain relevant evidence not found through hierarchical navigation.

=== Discovery Candidates ===
| ID | Path |
|:---|:-----|
{items}
=== End Discovery Candidates ===

User query: {query}
Select candidate IDs whose content is needed to answer the query.
If none are relevant, return an EMPTY list [].

Return ONLY a JSON object:
{{"selections": [{{"id": "D1", "confidence": <float>}}, ...]}}
Do not include any explanation.
"""


COLLECTOR_PROMPT = """\
You are a document navigation agent.

Document: "{doc_name}" (id: {doc_id})

{budget_block}

{trace_block}

Below is the document's section tree.
Nodes marked [Leaf] have no further sub-sections.
Nodes marked [✓] are already in your collection — do not re-collect them.
Token estimates (e.g. ~1.2k) show approximate content size.

=== Section Tree ===
{items_overview}
=== End Section Tree ===

User query: {query}

=== Rules ===

Each step you make THREE independent decisions:

{tools_block}

2. COLLECT — Add sections to your evidence collection (optional, can be empty).
   - COLLECT includes the section AND ALL its descendant content.
   - Set "outline": true to collect only structure (titles + summaries),
     keeping children available for further DRILL or COLLECT.
   - For [Leaf] nodes or small sections, prefer COLLECT over DRILL.
   - Do NOT re-collect paths already in your collection or marked [✓].

3. Navigate action — Where to go next (required, choose ONE):
    - DRILL — Expand a section to see its children in the next step.
      Use for larger sections when you want to be selective about sub-sections.
      You cannot DRILL into a path you just COLLECTed (already fully included).
      Target must be a path exactly as shown in the Section Tree — do NOT fabricate or shorten.
      NEVER drill into "{current_scope}" (current scope) or its ancestors.
      Target must NOT repeat any path in the Navigation Trace.
{back_rule}    - STOP — End navigation when you have enough evidence or nothing relevant remains.

=== End Rules ===

Return ONLY a JSON object:
{{"collect": [{{"path": "...", "confidence": <float>, "outline": false}}],
 "tools": ["SEARCH_IMAGES"], "tool_params": {{"search_query": "..."}},
 "action": "DRILL",
 "drill_into": "section/path",
 "reason": "..."}}
or
{{"collect": [...], "tools": [], "action": "STOP", "reason": "..."}}
Do not include any explanation outside the JSON.

IMPORTANT: 
1. All agent-generated text (e.g., "reason" and other free-text fields) MUST be written in English.
2. Document content and section paths MUST remain in their original language.
"""


def parse_collector_response(text: str) -> dict:
    """Parse the Collector Agent navigation response.

    Expected format:
    {"collect": [...], "action": "DRILL|BACK|STOP",
     "drill_into": "path", "tools": [...], "reason": "..."}
    """
    text = text.strip()
    asset_tools = {"SEARCH_IMAGES", "SEARCH_TABLES"}
    valid_actions = {"DRILL", "BACK", "STOP"}
    default: dict[str, Any] = {
        "collect": [], "action": "STOP", "drill_into": None,
        "tools": [], "reason": "",
    }

    def extract(data: dict) -> dict:
        action = str(data.get("action", "STOP")).strip().upper()
        if action not in valid_actions:
            action = "STOP"

        # Parse collect list
        collect_val = data.get("collect") or []
        collect: list[dict[str, Any]] = []
        if isinstance(collect_val, list):
            for item in collect_val:
                if isinstance(item, dict) and item.get("path"):
                    confidence = normalize_confidence(item.get("confidence", 0.7))
                    outline = bool(item.get("outline", False))
                    collect.append({
                        "path": str(item["path"]),
                        "confidence": confidence or 0.7,
                        "outline": outline,
                    })

        # Parse drill target
        drill_into = None
        if action == "DRILL":
            drill_into = data.get("drill_into")
            if isinstance(drill_into, str):
                drill_into = drill_into.strip() or None
            else:
                drill_into = None
            if drill_into is None:
                # No valid drill target → treat as STOP
                action = "STOP"

        # Parse tools
        tools_val = data.get("tools") or []
        tools: list[str] = []
        if isinstance(tools_val, list):
            tools = [
                str(t).strip().upper()
                for t in tools_val
                if str(t).strip().upper() in asset_tools
            ]

        # Parse tool parameters (search_query, chunk_id, etc.)
        tool_params: dict[str, Any] = {}
        raw_params = data.get("tool_params")
        if isinstance(raw_params, dict):
            tool_params = raw_params

        reason = str(data.get("reason") or "").strip()[:500]

        # Parse back_to target for BACK action
        back_to = None
        if action == "BACK":
            raw_back = data.get("back_to")
            if isinstance(raw_back, str) and raw_back.strip():
                back_to = raw_back.strip()
            # else: back_to remains None (= root)

        return {
            "collect": collect,
            "action": action,
            "drill_into": drill_into,
            "back_to": back_to,
            "tools": tools,
            "tool_params": tool_params,
            "reason": reason,
        }

    data = _parse_json_object(text)
    if data is not None:
        return extract(data)

    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        data = _parse_json_object(fence_match.group(1).strip())
        if data is not None:
            return extract(data)

    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        data = _parse_json_object(brace_match.group())
        if data is not None:
            return extract(data)

    return default


def parse_action_response(text: str) -> dict:
    """Parse discovery_select response (ID-based selections format)."""
    text = text.strip()
    default: dict[str, Any] = {"selections": []}

    def extract(data: dict) -> dict:
        selections_val = data.get("selections") or []
        selections: list[dict[str, Any]] = []
        if isinstance(selections_val, list):
            for selection in selections_val:
                if isinstance(selection, dict) and selection.get("id"):
                    confidence = normalize_confidence(selection.get("confidence", 0.7))
                    selections.append({
                        "id": str(selection["id"]).strip(),
                        "confidence": confidence or 0.7,
                    })
        return {"selections": selections}

    data = _parse_json_object(text)
    if data is not None:
        return extract(data)

    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        data = _parse_json_object(fence_match.group(1).strip())
        if data is not None:
            return extract(data)

    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        data = _parse_json_object(brace_match.group())
        if data is not None:
            return extract(data)

    return default


def format_budget_block(snapshot: dict | None) -> str:
    if not snapshot:
        return ""
    planning = snapshot.get("planning") or {}
    return (
        "=== Resource Status ===\n"
        f"Planning Budget: {planning.get('status', 'HEALTHY')} "
        f"({planning.get('used_pct', 0)}% used)\n"
        f"KG Coverage: {snapshot.get('explored_chunks', 0)}/"
        f"{snapshot.get('total_chunks', 0)} chunks explored\n"
        f"Docs Explored: {snapshot.get('explored_docs', 0)}/"
        f"{snapshot.get('total_docs', 0)}\n"
        "When budget is TIGHT, prefer fewer high-confidence selections over broad exploration. "
        "When CRITICAL, be very selective — only pick paths with strong relevance. Return empty if evidence suffices.\n"
        "=== End Resource Status ===\n"
    )


def parse_json_array(text: str) -> list[str]:
    """Best-effort extraction of a JSON array of strings from LLM response text."""
    result = extract_json_array_payload(text)
    return [str(item) for item in result]


def extract_json_array_payload(text: str) -> list[Any]:
    text = text.strip()
    result = _parse_json_array(text)
    if result is not None:
        return result
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if match:
        result = _parse_json_array(match.group())
        if result is not None:
            return result
    return []


def _fix_invalid_json_escapes(raw: str) -> str:
    """Fix invalid backslash escapes that LLMs produce from LaTeX paths.

    JSON only allows: \\", \\\\, \\/, \\b, \\f, \\n, \\r, \\t, \\uXXXX.
    LLMs often copy LaTeX like ``$3.0\\%$`` into JSON strings, producing
    invalid ``\\%``.  This replaces any ``\\X`` where X is NOT a valid
    JSON escape char with ``\\\\X`` so ``json.loads`` can succeed.
    """
    return re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw)


def _parse_json_object(raw_value: str) -> dict[str, Any] | None:
    try:
        result = json.loads(raw_value)
    except (ValueError, json.JSONDecodeError):
        # Retry with invalid-escape repair (common with LaTeX in PDF paths)
        try:
            result = json.loads(_fix_invalid_json_escapes(raw_value))
        except (ValueError, json.JSONDecodeError):
            return None
    return result if isinstance(result, dict) else None


def _parse_json_array(raw_value: str) -> list[Any] | None:
    try:
        result = json.loads(raw_value)
    except (ValueError, json.JSONDecodeError):
        return None
    return result if isinstance(result, list) else None


def normalize_confidence(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed > 1.0:
        parsed = parsed / 100.0
    return max(0.0, min(parsed, 1.0))
