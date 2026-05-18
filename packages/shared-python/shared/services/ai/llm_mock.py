"""Helpers for deterministic mock responses from OpenAI-compatible LLM calls."""

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


def _build_mock_response(task_name: str) -> str:
    """Return a canned response compatible with the inferred task contract."""
    response_by_task: Dict[str, str] = {
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
