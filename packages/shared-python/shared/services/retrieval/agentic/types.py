"""Core data types for agentic retrieval.

Defines the state machine primitives: configuration, state, actions,
observations, and tool results. All types are plain dataclasses with
no business logic — they are pure data containers.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ActionType(str, Enum):
    """Whitelisted action space for the retrieval agent."""
    BOTTOM_DISCOVERY = 'bottom_discovery'
    KG_DOCUMENT_SELECT = 'kg_document_select'
    DOCUMENT_PATH_SELECT = 'document_path_select'
    GREP_DOCUMENT_DISCOVER = 'grep_document_discover'
    GRAPH_EXPAND_DOCS = 'graph_expand_docs'
    DONE = 'done'  # Explicit termination signal from LLMPolicy


@dataclass
class AgentRunConfig:
    """Budget and limit configuration for a single agent run."""
    max_steps: int = 10
    max_docs: int = 0   # 0 = no limit, LLM decides autonomously
    max_path_expansions: int = 2
    max_doc_retries: int = 2
    latency_budget_ms: int = 12000
    min_evidence_paths: int = 1


@dataclass
class ToolResult:
    """Unified return type for all agentic tools.

    Every tool returns one of these, regardless of success or failure.
    The orchestrator reads ``status`` to decide next action.
    """
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    error: str | None = None


@dataclass
class CandidateDoc:
    """A document selected by kg_document_select."""
    document_id: str
    source_file_name: str = ''
    confidence: float = 0.0
    reason: str = ''
    source: str = ''  # 'kg_llm_select' | 'grep' | 'edge_expand'


@dataclass
class AgentState:
    """Mutable state carried through the agent loop.

    Updated by ``state.apply(action_type, tool_result)`` after each step.
    """
    # Timing
    start_time: float = field(default_factory=time.monotonic)
    step_count: int = 0

    # Discovery results (from bottom_discovery)
    discovery_paths: list[dict[str, Any]] = field(default_factory=list)
    discovery_top_doc_ids: list[str] = field(default_factory=list)
    discovery_done: bool = False

    # KG document selection
    selected_docs: list[CandidateDoc] = field(default_factory=list)
    excluded_doc_ids: set[str] = field(default_factory=set)
    doc_retry_count: int = 0
    kg_done: bool = False
    pending_doc_index: int = 0

    # Document path selection
    selected_paths: list[dict[str, Any]] = field(default_factory=list)
    path_expansion_count: int = 0

    # Last observation (used by policy to decide next action)
    last_observation: ToolResult | None = None

    # Context passed through
    doc_id_to_name: dict[str, str] = field(default_factory=dict)
    doc_job_map: dict[str, str] = field(default_factory=dict)

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.start_time) * 1000)

    def state_summary(self) -> dict[str, Any]:
        """Produce a concise state snapshot for the LLMPolicy prompt.

        Only includes fields the LLM needs for decision-making — never
        exposes raw chunk content or internal IDs verbatim.
        """
        selected_doc_summaries = [
            {
                'document_id': d.document_id,
                'name': d.source_file_name or '(unnamed)',
                'confidence': round(d.confidence, 2),
                'source': d.source,
            }
            for d in self.selected_docs
        ]
        selected_path_summaries = [
            {
                'path': p.get('path', ''),
                'confidence': round(float(p.get('confidence', 0.0) or 0.0), 2),
            }
            for p in self.selected_paths[:10]  # cap to avoid huge prompts
        ]
        last_obs = None
        if self.last_observation:
            last_obs = {
                'status': self.last_observation.status,
                'payload_keys': list(self.last_observation.payload.keys()),
                'error': self.last_observation.error,
            }
        return {
            'step': self.step_count,
            'discovery_done': self.discovery_done,
            'discovery_candidates': len(self.discovery_paths),
            'discovery_top_doc_ids': self.discovery_top_doc_ids[:5],
            'kg_done': self.kg_done,
            'selected_docs': selected_doc_summaries,
            'pending_doc_index': self.pending_doc_index,
            'selected_paths_count': len(self.selected_paths),
            'selected_paths': selected_path_summaries,
            'doc_retry_count': self.doc_retry_count,
            'last_observation': last_obs,
        }

    def apply(self, action_type: ActionType, result: ToolResult) -> None:
        """Update state based on action and its result."""
        self.last_observation = result

        if action_type == ActionType.BOTTOM_DISCOVERY:
            self.discovery_done = True
            if result.status != 'error':
                self.discovery_paths = result.payload.get('fused_rows', [])
                self.discovery_top_doc_ids = result.payload.get('top_doc_ids', [])

        elif action_type == ActionType.KG_DOCUMENT_SELECT:
            self.kg_done = True
            if result.status == 'selected_docs':
                new_docs = result.payload.get('candidate_docs', [])
                for doc_data in new_docs:
                    if isinstance(doc_data, CandidateDoc):
                        self.selected_docs.append(doc_data)
                    elif isinstance(doc_data, dict):
                        self.selected_docs.append(CandidateDoc(
                            document_id=doc_data.get('document_id', ''),
                            source_file_name=doc_data.get('source_file_name', ''),
                            confidence=doc_data.get('confidence', 0.0),
                            reason=doc_data.get('reason', ''),
                            source=doc_data.get('source', ''),
                        ))
                self.doc_id_to_name.update(result.payload.get('doc_id_to_name', {}))
                self.doc_job_map.update(result.payload.get('doc_job_map', {}))

                # Merge discovery hints
                existing_ids = {d.document_id for d in self.selected_docs}
                for did in self.discovery_top_doc_ids:
                    if did not in existing_ids and did not in self.excluded_doc_ids:
                        self.selected_docs.append(CandidateDoc(
                            document_id=did,
                            source_file_name=self.doc_id_to_name.get(did, ''),
                            confidence=0.5, # Lower than LLM's 0.8
                            reason='Bottom discovery hit',
                            source='discovery_hint',
                        ))
                        existing_ids.add(did)

        elif action_type == ActionType.DOCUMENT_PATH_SELECT:
            if result.status == 'selected_paths':
                new_paths = result.payload.get('selected_paths', [])
                self.selected_paths.extend(new_paths)
                self.pending_doc_index += 1
            elif result.status == 'no_items':
                self.pending_doc_index += 1
            elif result.status == 'need_more_docs':
                failed_doc_id = result.payload.get('document_id', '')
                if failed_doc_id:
                    self.excluded_doc_ids.add(failed_doc_id)
                self.doc_retry_count += 1
                self.kg_done = False  # allow re-entry to KG select
            elif result.status == 'no_confident_match':
                self.pending_doc_index += 1
            elif result.status == 'error':
                self.pending_doc_index += 1

        elif action_type == ActionType.GREP_DOCUMENT_DISCOVER:
            if result.status == 'discovered_docs':
                grep_doc_ids = result.payload.get('document_ids', [])
                doc_id_to_name = result.payload.get('doc_id_to_name', {})
                for did in grep_doc_ids:
                    if did not in self.excluded_doc_ids:
                        existing_ids = {d.document_id for d in self.selected_docs}
                        if did not in existing_ids:
                            self.selected_docs.append(CandidateDoc(
                                document_id=did,
                                source_file_name=doc_id_to_name.get(did, ''),
                                source='grep',
                            ))
                self.doc_id_to_name.update(doc_id_to_name)
                self.doc_job_map.update(result.payload.get('doc_job_map', {}))

        elif action_type == ActionType.GRAPH_EXPAND_DOCS:
            if result.status == 'expanded_docs':
                expanded_ids = result.payload.get('document_ids', [])
                doc_id_to_name = result.payload.get('doc_id_to_name', {})
                for did in expanded_ids:
                    if did not in self.excluded_doc_ids:
                        existing_ids = {d.document_id for d in self.selected_docs}
                        if did not in existing_ids:
                            self.selected_docs.append(CandidateDoc(
                                document_id=did,
                                source_file_name=doc_id_to_name.get(did, ''),
                                source='edge_expand',
                            ))
                self.doc_id_to_name.update(doc_id_to_name)
                self.doc_job_map.update(result.payload.get('doc_job_map', {}))
