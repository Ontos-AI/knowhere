"""LLM-driven policy for agentic retrieval.

Replaces the former RuleBasedPolicy.  All control decisions are made by a
small LLM call so the agent can adapt to query complexity and KB topology
rather than following a fixed rule tree.

Design:
  - ``LLMPolicy.decide()`` is **async** — it calls the LLM to pick the
    next action.
  - On parse failure the agent terminates immediately (no hidden fallback).
  - The prompt is compact: no raw chunk content, only state metadata.
"""
from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from shared.services.retrieval.agentic.types import ActionType, AgentRunConfig, AgentState
from shared.services.retrieval.llm_adapter import LLMFn

# ── Available actions presented to the LLM ───────────────────────────────────

_AVAILABLE_ACTIONS: list[dict[str, Any]] = [
    {
        'action': ActionType.BOTTOM_DISCOVERY.value,
        'description': (
            'Run BM25 3-channel bottom-layer discovery (path / content / term). '
            'Always call this first to get candidate chunks and top document hints.'
        ),
        'when': 'discovery_done is false',
    },
    {
        'action': ActionType.KG_DOCUMENT_SELECT.value,
        'description': (
            'Ask the LLM to select the most relevant documents from the Knowledge Graph '
            'overview (doc summaries). Use after discovery.'
        ),
        'when': 'discovery_done is true, kg_done is false',
    },
    {
        'action': ActionType.DOCUMENT_PATH_SELECT.value,
        'description': (
            'Drill into the next pending document\'s section tree and pick relevant '
            'paths. The document is chosen automatically from selected_docs[pending_doc_index].'
        ),
        'when': 'kg_done is true, there are pending documents to process',
    },
    {
        'action': ActionType.GREP_DOCUMENT_DISCOVER.value,
        'description': (
            'Run term/grep search across all documents to discover relevant doc IDs '
            'when KG selection found nothing.'
        ),
        'when': 'kg_done is true but selected_docs is empty',
    },
    {
        'action': ActionType.GRAPH_EXPAND_DOCS.value,
        'description': (
            'Expand via Knowledge Graph edge relationships to find related documents '
            'not yet selected.'
        ),
        'when': 'Optional deepening — use when current results seem insufficient',
    },
    {
        'action': ActionType.DONE.value,
        'description': (
            'Stop the agent loop and proceed to final hydration and ranking. '
            'Use when you have sufficient evidence paths or the budget is nearly exhausted.'
        ),
        'when': 'Sufficient paths collected, or further actions would not improve results',
    },
]

_ACTIONS_BLOCK = '\n'.join(
    f"  {i+1}. \"{a['action']}\": {a['description']} [{a['when']}]"
    for i, a in enumerate(_AVAILABLE_ACTIONS)
)

_POLICY_PROMPT_TEMPLATE = """\
You are a retrieval agent orchestrating document search for a RAG system.
Your job: choose the SINGLE best next action given the current state.

QUERY: "{query}"

CURRENT STATE:
{state_json}

BUDGET: {elapsed_ms}ms elapsed of {budget_ms}ms max | Step {step} of {max_steps}

AVAILABLE ACTIONS:
{actions_block}

RULES:
1. Always run bottom_discovery first (if discovery_done is false).
2. After discovery, run kg_document_select (if kg_done is false).
3. After kg select, run document_path_select for each pending document.
4. Call done when all pending documents are processed OR you have >= {min_evidence} evidence paths.
5. Only use grep_document_discover if kg_document_select found 0 documents.
6. Only use graph_expand_docs if you need more related docs after reviewing results.

Return ONLY a JSON object, no markdown, no explanation:
{{"action": "<action_name>", "reason": "<one sentence why>"}}
"""


def _parse_action_from_response(response: str) -> dict[str, Any] | None:
    """Extract the JSON decision object from the LLM response."""
    # Strip markdown code fences if present
    text = response.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s*```$', '', text, flags=re.MULTILINE)
    text = text.strip()

    # Try to extract {...} block
    match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # Try the whole text as JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


class LLMPolicy:
    """Async LLM-driven policy.  One LLM call per agent step.

    Usage::

        policy = LLMPolicy(llm_fn)
        action_type, reason = await policy.decide(state, config, query=query)
    """

    def __init__(self, llm_fn: LLMFn, *, query: str = '') -> None:
        self._llm_fn = llm_fn
        self._query = query

    def build_prompt(self, state: AgentState, config: AgentRunConfig) -> str:
        """Build the decision prompt.  Public for test inspection."""
        state_data = state.state_summary()
        state_json = json.dumps(state_data, ensure_ascii=False, indent=2)

        # Count pending docs

        return _POLICY_PROMPT_TEMPLATE.format(
            query=self._query,
            state_json=state_json,
            elapsed_ms=state.elapsed_ms,
            budget_ms=config.latency_budget_ms,
            step=state.step_count,
            max_steps=config.max_steps,
            actions_block=_ACTIONS_BLOCK,
            min_evidence=config.min_evidence_paths,
        )

    async def decide(
        self,
        state: AgentState,
        config: AgentRunConfig,
    ) -> tuple[ActionType | None, str]:
        """Ask the LLM which action to take next.

        Returns ``(action_type, reason)`` or ``(None, reason)`` when the
        LLM says ``done`` or when parsing fails (hard stop).
        """
        prompt = self.build_prompt(state, config)

        logger.info(
            f'  [LLMPolicy] step={state.step_count} calling LLM '
            f'(state: discovery={state.discovery_done}, kg={state.kg_done}, '
            f'docs={len(state.selected_docs)}, pending={state.pending_doc_index}, '
            f'paths={len(state.selected_paths)})'
        )

        # ── Verbose prompt logging (controlled by env) ──
        import os
        if os.environ.get('RETRIEVAL_AGENTIC_VERBOSE', 'false') == 'true':
            logger.info(
                f'\n{"="*60}\n'
                f'[LLMPolicy PROMPT step={state.step_count}]\n'
                f'{prompt}\n'
                f'{"="*60}'
            )

        raw_response = await self._llm_fn(prompt)

        logger.info(
            f'  [LLMPolicy] raw_response={repr(raw_response[:200])}'
        )

        if os.environ.get('RETRIEVAL_AGENTIC_VERBOSE', 'false') == 'true':
            logger.info(
                f'\n{"="*60}\n'
                f'[LLMPolicy RESPONSE step={state.step_count}]\n'
                f'{raw_response}\n'
                f'{"="*60}'
            )

        if not raw_response.strip():
            logger.warning('  [LLMPolicy] empty response → stopping agent')
            return None, 'empty LLM response'

        parsed = _parse_action_from_response(raw_response)
        if not parsed:
            logger.warning(
                f'  [LLMPolicy] could not parse JSON from response: {repr(raw_response[:300])} → stopping agent'
            )
            return None, f'parse_error: {raw_response[:100]}'

        action_str = str(parsed.get('action', '')).strip()
        reason = str(parsed.get('reason', '')).strip()

        logger.info(f'  [LLMPolicy] decided action="{action_str}" reason="{reason}"')

        if action_str == ActionType.DONE.value:
            return ActionType.DONE, reason

        # Validate against whitelist
        try:
            action_type = ActionType(action_str)
        except ValueError:
            logger.warning(
                f'  [LLMPolicy] unknown action "{action_str}" → stopping agent'
            )
            return None, f'unknown_action: {action_str}'

        return action_type, reason
