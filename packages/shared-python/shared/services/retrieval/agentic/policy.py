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
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from shared.services.retrieval.agentic.core.budget import BudgetExceeded
from shared.services.retrieval.agentic.core.types import AgentRunConfig, AgentState
from shared.services.retrieval.llm_adapter import LLMFn
from shared.utils.token_estimate import estimate_tokens


def _parse_answer_response(text: str) -> dict[str, Any] | None:
    """Extract a JSON answer object from LLM response text."""
    text = text.strip()
    parsed = _load_json_object(text)
    if parsed is not None:
        return parsed
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return _load_json_object(match.group())
    return None


def _load_json_object(raw_value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw_value)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _looks_like_json_wrapper(text: str) -> bool:
    """Detect malformed JSON-ish answer wrappers without exposing them."""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith('{') or stripped.endswith('}'):
        return True
    return bool(re.search(r'"(?:status|answer|reason)"\s*:', stripped))


def _is_external_http_url(url: str) -> bool:
    parsed = urlparse(str(url))
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return False
    host = parsed.hostname.strip().lower()
    if host in {'localhost', 'ip6-localhost', 'ip6-loopback'} or host.endswith('.local'):
        return False
    try:
        addr = ip_address(host)
    except ValueError:
        return True
    return not (addr.is_private or addr.is_loopback or addr.is_link_local)


def _budget_line_parts(budget_snapshot: dict | None, pool_name: str) -> dict[str, Any]:
    pool = ((budget_snapshot or {}).get(pool_name) or {})
    capacity = pool.get('capacity', 'unknown')
    remaining = pool.get('remaining', 'unknown')
    used_pct = pool.get('used_pct', 'unknown')
    remaining_pct: int | str = 'unknown'
    try:
        capacity_int = int(capacity)
        remaining_int = int(remaining)
        remaining_pct = 0 if capacity_int <= 0 else max(
            0,
            min(100, round(remaining_int * 100 / capacity_int)),
        )
    except (TypeError, ValueError):
        remaining_pct = 'unknown'
    return {
        'status': pool.get('status', 'HEALTHY'),
        'remaining': remaining,
        'capacity': capacity,
        'used_pct': used_pct,
        'remaining_pct': remaining_pct,
    }


async def attempt_answer(
    llm_fn: LLMFn,
    *,
    query: str,
    evidence_text: str,
    state: AgentState,
    config: AgentRunConfig,
    vlm_fn: LLMFn | None = None,
    image_urls: list[str] | None = None,
    budget_snapshot: dict | None = None,
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
    prompt_text = build_attempt_answer_prompt(
        query=query,
        evidence_text=evidence_text,
        state=state,
        config=config,
        budget_snapshot=budget_snapshot,
    )

    verbose = os.environ.get('RETRIEVAL_AGENTIC_VERBOSE', '') == 'true'

    # Decide whether to use VLM (multimodal) or text-only LLM
    effective_fn = llm_fn
    effective_input: Any = prompt_text

    usable_image_urls = [url for url in image_urls or [] if _is_external_http_url(url)]
    if image_urls and len(usable_image_urls) != len(image_urls):
        logger.info(
            f'  [attempt_answer] skipped {len(image_urls) - len(usable_image_urls)} '
            'non-public image URLs for VLM'
        )

    if vlm_fn and usable_image_urls:
        # Build multimodal message: text evidence + image_url parts
        content_parts: list[dict[str, Any]] = [
            {'type': 'text', 'text': prompt_text},
        ]
        for url in usable_image_urls[:20]:  # Cap at 20 images to avoid token overflow
            content_parts.append({
                'type': 'image_url',
                'image_url': {'url': url},
            })
        effective_input = [{'role': 'user', 'content': content_parts}]
        effective_fn = vlm_fn
        logger.info(
            f'  [attempt_answer] using VLM with {len(usable_image_urls)} image URLs '
            f'(capped at {min(len(usable_image_urls), 20)})'
        )

    if verbose:
        logger.info(
            f'[attempt_answer PROMPT]\n'
            f'{prompt_text}'
        )

    try:
        raw_response = await effective_fn(effective_input)
    except BudgetExceeded:
        raise
    except Exception as exc:
        if effective_fn is llm_fn:
            raise
        logger.warning(f'  [attempt_answer] VLM failed, falling back to text LLM: {exc}')
        raw_response = await llm_fn(prompt_text)
    logger.info(f'  [attempt_answer] raw={repr(raw_response[:300])}')

    if verbose:
        logger.info(
            f'[attempt_answer RESPONSE]\n'
            f'{raw_response}'
        )

    if not raw_response.strip() and effective_fn is not llm_fn:
        return 'NOT_FOUND', '', 'VLM returned empty response for multimodal evidence'

    parsed = _parse_answer_response(raw_response)
    if not parsed:
        if _looks_like_json_wrapper(raw_response):
            return 'NOT_FOUND', '', 'attempt_answer returned malformed JSON'
        # Keep plain-text fallback for providers that ignore JSON mode entirely.
        return 'DONE', raw_response.strip(), 'parse_error — treating plain text response as answer'

    status = str(parsed.get('status', 'DONE')).strip().upper()
    answer = str(parsed.get('answer', '')).strip()
    reason = str(parsed.get('reason', '')).strip()

    if status == 'NOT_FOUND':
        return 'NOT_FOUND', '', reason or 'LLM returned NOT_FOUND without reason'

    # Any status other than NOT_FOUND → treat as DONE
    if not answer:
        answer = reason or '(empty answer)'
    return 'DONE', answer, ''


def build_attempt_answer_prompt(
    *,
    query: str,
    evidence_text: str,
    state: AgentState,
    config: AgentRunConfig,
    budget_snapshot: dict | None = None,
) -> str:
    """Build the final answer prompt so trimming can estimate it beforehand."""
    planning = _budget_line_parts(budget_snapshot, 'planning')
    context = _budget_line_parts(budget_snapshot, 'context')
    return _ATTEMPT_ANSWER_PROMPT.format(
        query=query,
        evidence_context=evidence_text,
        revision_count=state.revision_count,
        max_revisions=config.max_revisions,
        planning_status=planning['status'],
        planning_remaining=planning['remaining'],
        planning_capacity=planning['capacity'],
        planning_used_pct=planning['used_pct'],
        planning_remaining_pct=planning['remaining_pct'],
        context_status=context['status'],
        context_remaining=context['remaining'],
        context_capacity=context['capacity'],
        context_used_pct=context['used_pct'],
        context_remaining_pct=context['remaining_pct'],
    )


def estimate_attempt_answer_prompt_tokens(
    *,
    query: str,
    evidence_text: str,
    state: AgentState,
    config: AgentRunConfig,
    budget_snapshot: dict | None = None,
) -> int:
    """Estimate the exact prompt shape that will be charged to context budget."""
    return estimate_tokens(build_attempt_answer_prompt(
        query=query,
        evidence_text=evidence_text,
        state=state,
        config=config,
        budget_snapshot=budget_snapshot,
    ))


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
Planning budget: {planning_status} ({planning_used_pct}% used, {planning_remaining_pct}% remaining, {planning_remaining}/{planning_capacity} remaining).
Context budget: {context_status} ({context_used_pct}% used, {context_remaining_pct}% remaining, {context_remaining}/{context_capacity} remaining); the evidence may have been trimmed.

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
