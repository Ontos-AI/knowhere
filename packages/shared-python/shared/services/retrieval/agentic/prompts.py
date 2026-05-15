"""Prompt templates and response parsers for agentic retrieval."""
from __future__ import annotations

import json
import re
from typing import Any


FILE_SELECT_PROMPT = """\
You are a document routing assistant.

{budget_block}
Below is a knowledge base overview showing all available documents,
their navigation summaries, chunk counts, and media counts.

=== Knowledge Base Overview ===
{overview}
=== End Overview ===

User query: {query}
{revision_context}
Based on the query, select documents that may contain relevant information.
If NO document in the knowledge base is relevant to the query, return an EMPTY array [].
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
{items}
=== End Discovery Candidates ===

User query: {query}
{revision_context}
Select section paths whose content is needed to answer the query.
If none are relevant, return an EMPTY list [].

Return ONLY a JSON object:
{{"selections": [{{"path": "...", "confidence": <float>}}, ...]}}
Do not include any explanation.
"""


ACTION_PROMPT = """\
You are a document navigation agent.

Document: "{doc_name}" (id: {doc_id})

{budget_block}
{scope_header}
Below is the document's section tree.
Sections tagged [SELECT] are within the current scope and may be selected.
Other sections are shown as structural context only (not selectable).
Nodes marked [Leaf] have no further sub-sections.

=== Section Tree ===
{items_overview}
=== End Section Tree ===

User query: {query}

=== Available Actions ===

Choose ONE action:

NAVIGATE — Drill into selected sections for detailed content.
  Consider this when the query targets specific topics and you need deeper text evidence.
  Select one or more [SELECT] sections.

STOP — Current scope evidence is sufficient. No further drill-down.
  Consider this when:
  - The query asks for an outline, overview, or summary
  - The query is broad/global, the tree section can fulfill it without drilling into individual sections.
  - You have already collected enough evidence at this level.

{tools_block}

When action is NAVIGATE, provide selections:
- You may ONLY select sections marked with [SELECT].

When action is STOP, selections must be empty.

Return ONLY a JSON object:
{{"action": "NAVIGATE", "tools": [...], "selections": [{{"path": "...", "confidence": <float>}}, ...]}}
or
{{"action": "STOP", "tools": [...], "selections": []}}
Do not include any explanation.
"""


def parse_action_response(text: str) -> dict:
    """Parse the unified navigation response from an LLM."""
    text = text.strip()
    asset_tools = {"FIND_IMAGES", "FIND_TABLES"}
    default = {"action": "NAVIGATE", "tools": [], "selections": []}

    def extract(data: dict) -> dict:
        action = str(data.get("action", "NAVIGATE")).strip().upper()
        if action not in ("NAVIGATE", "STOP"):
            action = "NAVIGATE"

        tools_val = data.get("tools") or []
        if isinstance(tools_val, list):
            tools = [
                str(tool).strip().upper()
                for tool in tools_val
                if str(tool).strip().upper() in asset_tools
            ]
        else:
            tools = []

        if action == "STOP":
            return {"action": action, "tools": tools, "selections": []}

        selections_val = data.get("selections") or []
        selections = []
        if isinstance(selections_val, list):
            for selection in selections_val:
                if isinstance(selection, dict) and selection.get("path"):
                    confidence = normalize_confidence(selection.get("confidence", 0.7))
                    selections.append({
                        "path": str(selection["path"]),
                        "confidence": confidence or 0.7,
                    })

        return {"action": action, "tools": tools, "selections": selections}

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return extract(data)
    except (ValueError, json.JSONDecodeError):
        pass

    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        try:
            data = json.loads(fence_match.group(1).strip())
            if isinstance(data, dict):
                return extract(data)
        except (ValueError, json.JSONDecodeError):
            pass

    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            data = json.loads(brace_match.group())
            if isinstance(data, dict):
                return extract(data)
        except (ValueError, json.JSONDecodeError):
            pass

    return default


def format_budget_block(snapshot: dict | None) -> str:
    if not snapshot:
        return ""
    planning = snapshot.get("planning") or {}
    context = snapshot.get("context") or {}
    return (
        "=== Resource Status ===\n"
        f"Planning Budget: {planning.get('status', 'HEALTHY')} "
        f"({planning.get('used_pct', 0)}% used)\n"
        f"Context Budget: {context.get('status', 'HEALTHY')} "
        f"({context.get('used_pct', 0)}% used)\n"
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
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, list):
                return result
        except (json.JSONDecodeError, ValueError):
            pass
    return []


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
