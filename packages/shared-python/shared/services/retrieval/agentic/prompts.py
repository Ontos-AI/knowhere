"""Prompt templates and response parsers for agentic retrieval."""
from __future__ import annotations

import json
import re
from typing import Any

from shared.services.retrieval.agentic.core.budget import project_budget_snapshot


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


COLLECTOR_PROMPT = """\
You are a document navigation agent running an observe-act loop.

Document: "{doc_name}" (id: {doc_id})

{agent_state_block}

{trace_block}

User query: {query}

{actionable_observation}

=== Rules ===

Each step chooses exactly ONE main action, plus optional COLLECT side effects.

Action semantics:
   - EXPAND observes a listed section's children in the next step.
   - COLLECT adds a listed section and all descendant content to evidence.
   - BACK only changes current scope; it does not collect evidence.
   - SEARCH_IMAGES and SEARCH_TABLES inspect assets in the current scope.
     Use only the listed SEARCH action ID. The asset inspector receives the
     user's original query directly.
     After a SEARCH result returns matches, use the matched assets and owner
     sections to decide whether more owner context is needed; avoid repeating
     the same asset search unless the current scope has changed and the prior
     result is insufficient for the query.
   - FINISH ends navigation for this document.

COLLECT side effect:
   - COLLECT includes the section AND ALL its descendant content.
   - Set "outline": true to collect only structure (titles + summaries),
     keeping children available for further EXPAND or COLLECT.
   - If you COLLECT the same section you EXPAND as the main action, use
     "outline": true so the section remains open for child exploration.
   - If the advisory query intent is MACRO_SUMMARY or STRUCTURE_OVERVIEW
     (document overview, chapter map, high-level summary), prefer outline
     collection; outline evidence can be sufficient final evidence.
   - If the advisory query intent is FACTUAL_DETAIL, NUMERIC_DETAIL, or
     ASSET_LOOKUP, prefer full evidence collection ("outline": false), or
     SEARCH_IMAGES/SEARCH_TABLES when visual/table evidence is central.
   - If the advisory query intent is UNKNOWN, decide from the user's wording:
     broad summaries can use outline, specific facts/numbers/assets need full
     evidence.
   - FINISH only when the collected evidence is sufficient for the user's query.
     The system will not infer missing evidence for you.
   - In CRITICAL budget mode, exploration is closed. Prefer the smallest
     sufficient COLLECT side effects, then FINISH.
   - In EXHAUSTED or overdraft budget mode, do not explore or search again.
     Use current observations/tool results to FINISH, or collect only
     indispensable visible evidence before FINISH.
   - For [Leaf] nodes or small sections, prefer COLLECT over EXPAND.

=== End Rules ===

Return ONLY a JSON object:
{{"collect": [{{"id": "C1", "confidence": <float>, "outline": false}}],
 "action": "<EXPAND|BACK|SEARCH_IMAGES|SEARCH_TABLES|FINISH>",
 "action_args": {{"id": "<main action ID>"}},
 "reason": "..."}}
Do not include any explanation outside the JSON.

IMPORTANT: 
1. All agent-generated text (e.g., "reason" and other free-text fields) MUST be written in English.
2. Document content and section paths MUST remain in their original language.
3. Use only action IDs from Actionable Observation. Never invent IDs or write raw section paths as action targets.
4. The action value MUST match the chosen ID group: E*=EXPAND, B*=BACK, S*=SEARCH, F*=FINISH.
5. When Budget mode is CRITICAL or EXHAUSTED, choose the best sufficient COLLECT side effects and then FINISH.
"""


QUERY_INTENT_PROMPT = """\
Classify the user's retrieval query for document navigation.

Return ONLY a JSON object: {{"intent": "<label>"}}

Allowed labels:
- MACRO_SUMMARY: asks for a broad summary, synthesis, overview, or "what is this document about"
- STRUCTURE_OVERVIEW: asks for chapters, outline, table of contents, hierarchy, or document structure
- FACTUAL_DETAIL: asks for specific facts, claims, definitions, clauses, or exact passages
- NUMERIC_DETAIL: asks for numbers, dates, prices, amounts, percentages, forecasts, metrics, or comparisons
- ASSET_LOOKUP: asks about images, figures, charts, tables, screenshots, or visual/table evidence
- UNKNOWN: unclear or mixed intent

User query: {query}
"""


QUERY_INTENT_LABELS = {
    "MACRO_SUMMARY",
    "STRUCTURE_OVERVIEW",
    "FACTUAL_DETAIL",
    "NUMERIC_DETAIL",
    "ASSET_LOOKUP",
    "UNKNOWN",
}


def parse_query_intent_response(text: str) -> str:
    data = _parse_json_object(text.strip())
    if not isinstance(data, dict):
        return "UNKNOWN"
    intent = str(data.get("intent") or "").strip().upper()
    return intent if intent in QUERY_INTENT_LABELS else "UNKNOWN"


def parse_collector_response(text: str) -> dict:
    """Parse the Collector Agent navigation response.

    Expected format:
    {"collect": [{"id": "C1", ...}], "action": "EXPAND|BACK|FINISH|SEARCH_IMAGES|SEARCH_TABLES",
     "action_args": {"id": "E1"}, "reason": "..."}
    """
    text = text.strip()
    valid_actions = {"EXPAND", "BACK", "FINISH", "SEARCH_IMAGES", "SEARCH_TABLES"}
    default: dict[str, Any] = {
        "collect": [], "action": "ERROR", "action_id": None,
        "tools": [], "tool_params": {}, "reason": "invalid model response",
    }

    def extract(data: dict) -> dict:
        action = str(data.get("action", "ERROR")).strip().upper()
        if action not in valid_actions:
            action = "ERROR"
        action_args = data.get("action_args")
        if not isinstance(action_args, dict):
            action_args = {}

        # Parse collect list
        collect_val = data.get("collect") or []
        collect: list[dict[str, Any]] = []
        if isinstance(collect_val, list):
            for item in collect_val:
                if isinstance(item, dict) and item.get("id"):
                    confidence = normalize_confidence(item.get("confidence", 0.7))
                    outline = bool(item.get("outline", False))
                    collect.append({
                        "id": str(item["id"]).strip(),
                        "confidence": confidence or 0.7,
                        "outline": outline,
                    })

        action_id = action_args.get("id")
        if isinstance(action_id, str):
            action_id = action_id.strip() or None
        else:
            action_id = None

        # SEARCH tools use the original user query. We intentionally ignore
        # model-generated query rewrites here so navigation cannot silently
        # broaden or narrow the asset inspector's task.
        tool_params: dict[str, Any] = {}
        if action in {"SEARCH_IMAGES", "SEARCH_TABLES"}:
            tools = [action]
        else:
            tools = []

        reason = str(data.get("reason") or "").strip()[:500]

        return {
            "collect": collect,
            "action": action,
            "action_id": action_id,
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


def adjust_budget_snapshot(
    snapshot: dict | None,
    additional_tokens: int,
) -> dict | None:
    """Adjust a budget snapshot by adding estimated tokens for the current call.

    This ensures the LLM sees the budget state *after* this call's cost,
    not before, preventing misleadingly low percentages.
    """
    return project_budget_snapshot(
        snapshot,
        pool="planning",
        additional_tokens=additional_tokens,
    )


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
