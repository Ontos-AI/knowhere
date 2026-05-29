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
{items}
=== End Discovery Candidates ===

User query: {query}
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
Sections tagged [SELECT] are the recommended selection granularity for this scope.
Other visible sections are structural context and may be selected when you need to drill into that broader scope.
Nodes marked [Leaf] have no further sub-sections.

=== Section Tree ===
{items_overview}
=== End Section Tree ===

User query: {query}

=== Available Actions ===

Choose ONE action:

NAVIGATE — Drill into selected sections for detailed content.
  Consider this when the query targets specific topics and you need deeper text evidence.
  Prefer one or more [SELECT] sections, or choose a broader visible section when needed.

STOP — Current scope evidence is sufficient. No further drill-down.
  Consider this when:
  - The query asks for an outline, overview, or summary
  - The query is broad/global, the tree section can fulfill it without drilling into individual sections.
  - You have already collected enough evidence at this level.

{tools_block}

When action is NAVIGATE, provide selections:
- Select visible section paths from the tree above; prefer [SELECT] paths when they fit.

When action is STOP, selections must be empty.

Always include a "reason" field (1-2 sentences) explaining your choice.
When action is STOP, also include "stop_type" from: sufficient_outline, no_relevant_child, evidence_sufficient, budget_conserve.

Return ONLY a JSON object:
{{"action": "NAVIGATE", "reason": "...", "tools": [...], "selections": [{{"path": "...", "confidence": <float>}}, ...]}}
or
{{"action": "STOP", "reason": "...", "stop_type": "...", "tools": [...], "selections": []}}
Do not include any explanation.
"""


def parse_action_response(text: str) -> dict:
    """Parse the unified navigation response from an LLM."""
    text = text.strip()
    asset_tools = {"FIND_IMAGES", "FIND_TABLES"}
    default = {"action": "NAVIGATE", "tools": [], "selections": [], "reason": "", "stop_type": ""}

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

        reason = str(data.get("reason") or "").strip()[:500]
        stop_type = str(data.get("stop_type") or "").strip()[:50] if action == "STOP" else ""

        if action == "STOP":
            return {"action": action, "tools": tools, "selections": [], "reason": reason, "stop_type": stop_type}

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

        return {"action": action, "tools": tools, "selections": selections, "reason": reason, "stop_type": ""}

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


def _parse_json_object(raw_value: str) -> dict[str, Any] | None:
    try:
        result = json.loads(raw_value)
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
