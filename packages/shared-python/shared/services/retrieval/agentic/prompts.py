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
You are a document navigation agent running an observe-act loop.

Document: "{doc_name}" (id: {doc_id})

{budget_block}

{trace_block}

Below is your current observation of the document section tree.
Nodes marked [Leaf] have no further sub-sections.
Nodes marked [✓] are already collected as evidence.
Nodes marked [seen] were already expanded/observed.
Token estimates (e.g. ~1.2k) show approximate content size.

=== Section Tree ===
{items_overview}
=== End Section Tree ===

User query: {query}

=== Rules ===

Each step chooses exactly ONE main action, plus optional COLLECT side effects.

{tools_block}

Navigation state:
   - Current scope: "{current_scope}"
   - EXPAND means observing a scope's children. Do not EXPAND [seen], [✓],
     the current scope, or any ancestor of the current scope.
   - COLLECT means adding a section and all descendant content to evidence.
     Do not COLLECT [✓] paths or descendants of [✓] paths.
   - Treat already expanded scopes and fully collected paths as processed:
     do not spend another action on processed content.
   - BACK only changes the current scope; it does not collect evidence.

COLLECT side effect — Add sections to your evidence collection (optional, can be empty).
   - COLLECT includes the section AND ALL its descendant content.
   - Set "outline": true to collect only structure (titles + summaries),
     keeping children available for further EXPAND or COLLECT.
   - For [Leaf] nodes or small sections, prefer COLLECT over EXPAND.

Available main actions — choose ONE:
{main_actions_block}

=== End Rules ===

Return ONLY a JSON object:
{{"collect": [{{"path": "...", "confidence": <float>, "outline": false}}],
 "action": "EXPAND",
 "action_args": {{"target": "section/path"}},
 "reason": "..."}}
or
{{"collect": [...],
 "action": "SEARCH_TABLES",
 "action_args": {{"query": "..."}},
 "reason": "..."}}
or
{{"collect": [...], "action": "FINISH", "reason": "..."}}
Do not include any explanation outside the JSON.

IMPORTANT: 
1. All agent-generated text (e.g., "reason" and other free-text fields) MUST be written in English.
2. Document content and section paths MUST remain in their original language.
"""


def parse_collector_response(text: str) -> dict:
    """Parse the Collector Agent navigation response.

    Expected format:
    {"collect": [...], "action": "EXPAND|BACK|FINISH|SEARCH_IMAGES|SEARCH_TABLES",
     "action_args": {"target": "...", "query": "..."}, "reason": "..."}
    """
    text = text.strip()
    valid_actions = {"EXPAND", "BACK", "FINISH", "SEARCH_IMAGES", "SEARCH_TABLES"}
    default: dict[str, Any] = {
        "collect": [], "action": "FINISH", "drill_into": None,
        "tools": [], "tool_params": {}, "reason": "",
    }

    def extract(data: dict) -> dict:
        action = str(data.get("action", "FINISH")).strip().upper()
        if action not in valid_actions:
            action = "FINISH"
        action_args = data.get("action_args")
        if not isinstance(action_args, dict):
            action_args = {}

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
        if action == "EXPAND":
            drill_into = action_args.get("target")
            if isinstance(drill_into, str):
                drill_into = drill_into.strip() or None
            else:
                drill_into = None
            if drill_into is None:
                action = "FINISH"

        # Parse tool parameters.
        tool_params: dict[str, Any] = {}
        if action in {"SEARCH_IMAGES", "SEARCH_TABLES"}:
            query = action_args.get("query") or action_args.get("search_query")
            if isinstance(query, str) and query.strip():
                tool_params["search_query"] = query.strip()
            tools = [action]
        else:
            tools = []

        reason = str(data.get("reason") or "").strip()[:500]

        # Parse back_to target for BACK action
        back_to = None
        if action == "BACK":
            raw_back = action_args.get("target")
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


def adjust_budget_snapshot(
    snapshot: dict | None,
    additional_tokens: int,
) -> dict | None:
    """Adjust a budget snapshot by adding estimated tokens for the current call.

    This ensures the LLM sees the budget state *after* this call's cost,
    not before, preventing misleadingly low percentages.
    """
    if not snapshot:
        return snapshot
    import copy
    adjusted = copy.deepcopy(snapshot)
    planning = adjusted.get("planning")
    if not planning:
        return adjusted
    capacity = planning.get("capacity", 1)
    used = planning.get("used", 0) + additional_tokens
    used_pct = min(int(used * 100 / capacity), 100) if capacity > 0 else 100
    planning["used"] = used
    planning["used_pct"] = used_pct
    planning["remaining"] = max(0, capacity - used)
    if used_pct >= 90:
        planning["status"] = "EXHAUSTED"
    elif used_pct >= 75:
        planning["status"] = "CRITICAL"
    elif used_pct >= 50:
        planning["status"] = "TIGHT"
    else:
        planning["status"] = "HEALTHY"
    return adjusted


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
