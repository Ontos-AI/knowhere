"""LLM answer-attempt tool for agentic retrieval.

Provides ``attempt_answer()`` — a single LLM call that tries to answer
the user's query using the collected evidence.

Returns one of two outcomes:
  - answer_text (non-empty) → the evidence was sufficient, answer is ready
  - NOT_FOUND + reason → the evidence was insufficient, triggers a revision
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from loguru import logger

from shared.services.retrieval.agentic.types import AgentRunConfig, AgentState
from shared.services.retrieval.llm_adapter import LLMFn


def _parse_answer_response(text: str) -> dict[str, Any] | None:
    """Extract a JSON answer object from LLM response text."""
    text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except (json.JSONDecodeError, ValueError):
            pass
    return None


async def attempt_answer(
    llm_fn: LLMFn,
    *,
    query: str,
    evidence_text: str,
    state: AgentState,
    config: AgentRunConfig,
    vlm_fn: LLMFn | None = None,
    image_urls: list[str] | None = None,
) -> tuple[str, str, str]:
    """Attempt to answer the query using collected evidence.

    When ``vlm_fn`` is provided and ``image_urls`` is non-empty, the
    answer is generated using the VLM in multimodal message format
    (text + image_url parts) so the model can actually *see* chart
    images rather than only reading their text descriptions.

    Falls back to ``llm_fn`` (text-only) when VLM is unavailable or
    when no images are present.

    Returns (status, answer_text, reason) where:
      - status='DONE', answer_text=<answer>, reason=''
        → evidence was sufficient, answer is ready
      - status='NOT_FOUND', answer_text='', reason=<why>
        → evidence was insufficient, reason is used as revision_hint
    """
    prompt_text = _ATTEMPT_ANSWER_PROMPT.format(
        query=query,
        evidence_context=evidence_text,
        revision_count=state.revision_count,
        max_revisions=config.max_revisions,
    )

    verbose = os.environ.get('RETRIEVAL_AGENTIC_VERBOSE', '') == 'true'

    # Decide whether to use VLM (multimodal) or text-only LLM
    effective_fn = llm_fn
    effective_input: Any = prompt_text

    if vlm_fn and image_urls:
        # Build multimodal message: text evidence + image_url parts
        content_parts: list[dict[str, Any]] = [
            {'type': 'text', 'text': prompt_text},
        ]
        for url in image_urls[:20]:  # Cap at 20 images to avoid token overflow
            content_parts.append({
                'type': 'image_url',
                'image_url': {'url': url},
            })
        effective_input = [{'role': 'user', 'content': content_parts}]
        effective_fn = vlm_fn
        logger.info(
            f'  [attempt_answer] using VLM with {len(image_urls)} image URLs '
            f'(capped at {min(len(image_urls), 20)})'
        )

    if verbose:
        logger.info(
            f'[attempt_answer PROMPT]\n'
            f'{prompt_text}'
        )

    raw_response = await effective_fn(effective_input)
    logger.info(f'  [attempt_answer] raw={repr(raw_response[:300])}')

    if verbose:
        logger.info(
            f'[attempt_answer RESPONSE]\n'
            f'{raw_response}'
        )

    # If VLM returned empty (e.g. URL not reachable), fall back to text LLM
    if not raw_response.strip() and effective_fn is not llm_fn:
        logger.info('  [attempt_answer] VLM returned empty, falling back to text LLM')
        raw_response = await llm_fn(prompt_text)
        logger.info(f'  [attempt_answer] text fallback raw={repr(raw_response[:300])}')

    parsed = _parse_answer_response(raw_response)
    if not parsed:
        # Parse error: treat raw text as best-effort answer
        return 'DONE', raw_response.strip(), 'parse_error — treating raw response as answer'

    status = str(parsed.get('status', 'DONE')).strip().upper()
    answer = str(parsed.get('answer', '')).strip()
    reason = str(parsed.get('reason', '')).strip()

    if status == 'NOT_FOUND':
        return 'NOT_FOUND', '', reason or 'LLM returned NOT_FOUND without reason'

    # Any status other than NOT_FOUND → treat as DONE
    if not answer:
        answer = reason or '(empty answer)'
    return 'DONE', answer, ''


_ATTEMPT_ANSWER_PROMPT = """\
You are a knowledge retrieval assistant. Answer the user's query based
STRICTLY on the provided evidence. Do NOT use any external knowledge.

QUERY: "{query}"

EVIDENCE CONTEXT:
The following evidence is organized by document in a unified hierarchy.
Each document shows its structural outline (section titles + summaries)
with retrieved content (┈ lines) inline under the relevant sections.

{evidence_context}

REVISION: {revision_count} of {max_revisions} revisions used.

INSTRUCTIONS:
1. If the evidence contains enough information to answer the query,
   compose a clear and comprehensive answer. Return:
   {{"status": "DONE", "answer": "<your answer here>"}}

2. If the evidence does NOT contain sufficient information to answer
   the query, return NOT_FOUND with a specific reason explaining what
   information is missing. This reason will be used to guide the next
   search round. Return:
   {{"status": "NOT_FOUND", "reason": "<what specific information is missing>"}}

IMPORTANT:
- Base your judgment ONLY on the actual retrieved content (┈ lines),
  not just section titles or summaries.
- Be specific in your NOT_FOUND reason — mention exactly what data,
  section, or detail you expected but didn't find.
- When the evidence partially covers the query, still return DONE with
  the available information and note any gaps in your answer.

Return ONLY a JSON object, no other text.
"""
