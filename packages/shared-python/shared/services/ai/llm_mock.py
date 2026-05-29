"""Helpers for deterministic mock responses from OpenAI-compatible LLM calls."""

import json
import re
from typing import Any, Dict, List

from loguru import logger
from openai.types.chat import ChatCompletionMessageParam


def build_mock_chat_completion_response(
    messages: List[ChatCompletionMessageParam],
    model_name: str,
) -> str:
    """Return a canned response compatible with the inferred prompt contract."""
    prompt_text = _extract_prompt_text(messages)
    task_name = _detect_mock_task(prompt_text)
    logger.debug(
        "LLM mock enabled, returning canned response: model={}, task={}",
        model_name,
        task_name,
    )
    # For agentic tasks that need dynamic content extraction, build the response here.
    if task_name == "agentic-planner":
        return _build_planner_mock_response(prompt_text)
    if task_name == "agentic-navigate":
        return _build_navigate_mock_response(prompt_text)
    if task_name == "agentic-discovery-select":
        return _build_discovery_select_mock_response(prompt_text)
    return _build_mock_response(task_name)


def _extract_message_text(content: Any) -> str:
    """Flatten chat message content into plain text for mock-task detection."""
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return ""

    parts: List[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue

        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        if item_type == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
            continue

        if isinstance(item.get("content"), str):
            parts.append(item["content"])

    return "\n".join(parts)


def _extract_prompt_text(messages: List[ChatCompletionMessageParam]) -> str:
    """Extract all text segments from a chat-completion message list."""
    parts: List[str] = []

    for message in messages:
        if not isinstance(message, dict):
            continue

        text_part = _extract_message_text(message.get("content"))
        if text_part:
            parts.append(text_part)

    return "\n".join(parts)


def _detect_mock_task(prompt_text: str) -> str:
    """Infer the prompt task so the mock can return a compatible response shape."""
    normalized_prompt = prompt_text.lower()

    # ── Agentic retrieval prompts (check first — they are structurally distinct) ──
    if (
        "you are a retrieval workflow planner" in normalized_prompt
        and "concat_final_parts" in normalized_prompt
    ):
        return "agentic-planner"
    if (
        "you are a document navigation agent" in normalized_prompt
        and "=== section tree ==" in normalized_prompt
    ):
        return "agentic-navigate"
    if (
        "=== discovery candidates ==" in normalized_prompt
        and "\"selections\"" in normalized_prompt
    ):
        return "agentic-discovery-select"

    # ── Document parsing / ingestion prompts ──
    if (
        "generate a concise title" in normalized_prompt
        and "return only the title" in normalized_prompt
    ):
        return "fragment-title"
    if (
        '"toc_start"' in normalized_prompt
        and '"toc_end"' in normalized_prompt
        and '"confidence"' in normalized_prompt
    ):
        return "detect-toc-range"
    if (
        "row numbers where headers are located" in normalized_prompt
        and '"answer"' in normalized_prompt
    ):
        return "detect-table-headers"
    if '"answer"' in normalized_prompt and '"text" or "image"' in normalized_prompt:
        return "judge-image-type"
    if (
        '"title"' in normalized_prompt
        and '"keywords"' in normalized_prompt
        and '"summary"' in normalized_prompt
    ):
        return "summary-full"
    if (
        'json dictionary format with key "answer"' in normalized_prompt
        and "keywords" in normalized_prompt
    ):
        return "summary-keywords"
    if (
        "json array only" in normalized_prompt
        and "toc, not body text" in normalized_prompt
    ):
        return "eval-toc-headings"
    if (
        "json array" in normalized_prompt
        and "only include rows that you judge to be headings" in normalized_prompt
    ):
        return "eval-headings"
    if "scanned page from an engineering atlas" in normalized_prompt:
        return "atlas-page-info"
    if "perform ocr operation" in normalized_prompt:
        return "ocr-image"
    if (
        "you will receive an image" in normalized_prompt
        and "line 1: output a short title" in normalized_prompt
    ):
        return "summary-images"
    if (
        "you will receive one or more images and the user's current question"
        in normalized_prompt
    ):
        return "ask-image"
    if "summaries of sub-sections from a document section" in normalized_prompt:
        return "file-summary"
    if (
        "line 1: output a short title" in normalized_prompt
        and "line 2 onward" in normalized_prompt
    ):
        return "summary-titled"
    if "extract the main content of the material" in normalized_prompt:
        return "summary"

    return "default"


def _extract_first_section_path(prompt_text: str) -> str | None:
    """Pull the first path value from a COLLECTOR_PROMPT section tree block.

    The section tree is rendered by section_prompt_projection.format_items_for_llm()
    and each item line looks like::

        ▸ [L1] path="Root" [text=1] ~100 tokens [Leaf]
        └ [L2] path="Root / Sub" [text=2] ~200 tokens

    We extract the value inside path="..." from the first matching line
    within the === Section Tree === block.
    """
    tree_match = re.search(
        r"=== Section Tree ===(.*?)=== End Section Tree ===",
        prompt_text,
        re.DOTALL | re.IGNORECASE,
    )
    if not tree_match:
        return None
    tree_block = tree_match.group(1)
    # Match path="..." in the section tree — this is the canonical format
    path_match = re.search(r'path="([^"]+)"', tree_block)
    if path_match:
        return path_match.group(1)
    return None



def _extract_user_query(prompt_text: str) -> str:
    """Extract the user query line from a planner/navigation prompt.

    The PLANNER_PROMPT and COLLECTOR_PROMPT both contain::
        User query: {query}
    """
    match = re.search(r"User query:\s*(.+)", prompt_text)
    if match:
        return match.group(1).strip()
    return "mock query"


def _build_planner_mock_response(prompt_text: str) -> str:
    """Return a valid single-step QueryPlan JSON using the real query from the prompt."""
    query = _extract_user_query(prompt_text)
    response = {
        "reasoning_summary": "mock single-step plan",
        "steps": [
            {
                "id": "s1",
                "sub_query": query,
                "step_kind": "retrieve",
                "depends_on": [],
                "output_role": "final_part",
                "top_k": 10,
            }
        ],
        "final_strategy": "concat_final_parts",
    }
    return json.dumps(response)


def _build_navigate_mock_response(prompt_text: str) -> str:
    """Return a mock COLLECTOR_PROMPT response that COLLECTs the first visible path."""
    path = _extract_first_section_path(prompt_text)
    if path:
        response = {
            "collect": [{"path": path, "confidence": 0.9, "outline": False}],
            "action": "STOP",
            "drill_into": None,
            "tools": [],
            "reason": "Mock: collected first available section",
        }
    else:
        # No path found — STOP without collecting (safe fallback)
        response = {
            "collect": [],
            "action": "STOP",
            "drill_into": None,
            "tools": [],
            "reason": "Mock: no section path found in tree",
        }
    return json.dumps(response)


def _extract_first_discovery_path(prompt_text: str) -> str | None:
    """Pull the first path value from a DISCOVERY_SELECT_PROMPT candidates block.

    Discovery hints are rendered by selection._project_discovery_hints() as::

        ▸ path="Findings"
            <summary text>

    We extract the value inside path="..." from the candidates block.
    """
    candidates_match = re.search(
        r"=== Discovery Candidates ===(.*?)=== End Discovery Candidates ===",
        prompt_text,
        re.DOTALL | re.IGNORECASE,
    )
    if not candidates_match:
        return None
    block = candidates_match.group(1)
    # Match path="..." — same canonical format as section tree
    path_match = re.search(r'path="([^"]+)"', block)
    if path_match:
        return path_match.group(1)
    return None


def _build_discovery_select_mock_response(prompt_text: str) -> str:
    """Return a mock DISCOVERY_SELECT_PROMPT response selecting the first candidate."""
    path = _extract_first_discovery_path(prompt_text)
    if path:
        response = {"selections": [{"path": path, "confidence": 0.85}]}
    else:
        response = {"selections": []}
    return json.dumps(response)


def _build_mock_response(task_name: str) -> str:
    """Return a canned response compatible with the inferred task contract."""
    response_by_task: Dict[str, str] = {
        # Agentic retrieval — static fallbacks (dynamic responses built elsewhere)
        "agentic-planner": (
            '{"reasoning_summary": "mock single-step plan", '
            '"steps": [{"id": "s1", "sub_query": "mock query", '
            '"step_kind": "retrieve", "depends_on": [], '
            '"output_role": "final_part", "top_k": 10}], '
            '"final_strategy": "concat_final_parts"}'
        ),
        # Document parsing / ingestion tasks
        "fragment-title": "Mock Fragment Title",
        "detect-toc-range": '{"toc_start": null, "toc_end": null, "confidence": "low"}',
        "detect-table-headers": '{"answer": [0]}',
        "judge-image-type": '{"answer": "image"}',
        "summary-full": '{"title": "Mock Title", "keywords": "mock", "summary": "Mock summary"}',
        "summary-keywords": '{"answer": "mock"}',
        "eval-headings": "[]",
        "eval-toc-headings": "[]",
        "atlas-page-info": "Mock atlas page info",
        "ocr-image": "Mock OCR text",
        "summary-images": "Mock Image Title\nMock image summary",
        "ask-image": "Mock image answer",
        "file-summary": "Mock section summary",
        "summary-titled": "Mock Title\nMock summary",
        "summary": "Mock summary",
        "default": "Mock LLM response",
    }
    return response_by_task.get(task_name, response_by_task["default"])
