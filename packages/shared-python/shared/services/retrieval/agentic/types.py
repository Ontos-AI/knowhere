"""Core data types for agentic retrieval.

Defines the state machine primitives: configuration, state, actions,
observations, and tool results. All types are plain dataclasses with
no business logic — they are pure data containers.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentRunConfig:
    """Budget and limit configuration for a single agent run."""
    max_revisions: int = 2  # max attempt_answer → revision cycles
    max_nav_depth: int = 3  # max scope_navigate recursion depth
    latency_budget_ms: int = 12000


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
class DocTreeNode:
    """Unified navigation result tree for one document.

    Produced by ``scope_navigate_step``. Captures the full
    navigation outcome for rendering as a single hierarchy:

    - ``outline_items``: section tree items at this scope level
    - ``leaf_content``: hydrated chunk rows keyed by section path
      (leaf selections from LLM)
    - ``children``: recursive child trees keyed by section path
      (non-leaf selections, populated by orchestrator BFS queue)
    - ``confidence``: per-selection confidence for trimming
    """
    scope_path: str | None = None

    # Outline items at this level (title + summary)
    outline_items: list[dict[str, Any]] = field(default_factory=list)

    # Leaf results, keyed by section path:
    leaf_content: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    children: dict[str, 'DocTreeNode'] = field(default_factory=dict)

    # Confidence per selection (for trimming)
    confidence: dict[str, float] = field(default_factory=dict)

    @staticmethod
    def empty(scope_path: str | None = None) -> 'DocTreeNode':
        return DocTreeNode(scope_path=scope_path)

    def has_content(self) -> bool:
        """Check if this tree has any meaningful content (outline, chunks, or children)."""
        if self.outline_items:
            return True
        if self.leaf_content:
            return True
        if self.children:
            return any(c.has_content() for c in self.children.values())
        return False

    def collect_all_paths(self, doc_id: str) -> set[str]:
        """Recursively collect paths of actually-explored sections.

        Only ``leaf_content`` and ``children`` represent sections whose
        content was retrieved.  ``outline_items`` are structural context
        (titles/summaries shown to the LLM) and must NOT be masked —
        otherwise revision rounds see 0 candidates and can't re-navigate.
        """
        paths: set[str] = set()
        if self.scope_path:
            paths.add(f'{doc_id}::{self.scope_path}')
        for path in self.leaf_content:
            paths.add(f'{doc_id}::{path}')
        for path, child in self.children.items():
            paths.add(f'{doc_id}::{path}')
            paths.update(child.collect_all_paths(doc_id))
        return paths

    def flatten_chunk_rows(self) -> list[dict[str, Any]]:
        """Recursively collect all hydrated chunk rows (document order)."""
        rows: list[dict[str, Any]] = []
        for chunks in self.leaf_content.values():
            rows.extend(chunks)
        for child in self.children.values():
            rows.extend(child.flatten_chunk_rows())
        return rows

    def collect_referenced_ids(self) -> list[dict[str, str]]:
        """Extract minimal chunk references from all hydrated leaves.

        Returns deduplicated list of {chunk_id, document_id, chunk_type,
        section_path, file_path, job_id} for hit stats and frontend display.
        """
        refs: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in self.flatten_chunk_rows():
            cid = row.get('chunk_id', '')
            if cid and cid not in seen:
                seen.add(cid)
                refs.append({
                    'chunk_id': cid,
                    'document_id': row.get('document_id', ''),
                    'chunk_type': row.get('chunk_type', ''),
                    'section_path': row.get('section_path', ''),
                    'file_path': row.get('file_path', ''),
                    'job_id': row.get('job_id', ''),
                })
        return refs

    def merge(self, other: 'DocTreeNode') -> None:
        """Additive merge for revision cycles.

        Merges outline items, leaf content, children, and confidence from
        ``other`` into this node. Existing data is preserved; new data is
        added. For confidence values, the higher value wins.
        """
        existing_paths = {item['path'] for item in self.outline_items}
        for item in other.outline_items:
            if item.get('path', '') not in existing_paths:
                self.outline_items.append(item)
        for path, chunks in other.leaf_content.items():
            if path not in self.leaf_content:
                self.leaf_content[path] = chunks
        for path, child in other.children.items():
            if path in self.children:
                self.children[path].merge(child)
            else:
                self.children[path] = child
        for path, conf in other.confidence.items():
            self.confidence[path] = max(self.confidence.get(path, 0), conf)


@dataclass
class CandidateDoc:
    """A document selected by kg_document_select."""
    document_id: str
    source_file_name: str = ''
    confidence: float = 0.0
    reason: str = ''
    source: str = ''  # 'kg_llm_select' | 'grep' | 'edge_expand' | 'discovery_hint'


@dataclass
class AgenticResult:
    """Output of agentic retrieval.

    - ``evidence_text``: complete hierarchical context for LLM answering
      (rendered doc tree with outline + leaf content + inline tables)
    - ``answer_text``: LLM-generated answer to the query based on the
      evidence.  Empty string when the evidence was insufficient
      (NOT_FOUND) and max revisions were exhausted.
    - ``referenced_chunks``: minimal chunk references for hit stats
      and frontend display (chunk_id, document_id, chunk_type, etc.)
    - ``router_used``: routing path identifier
    """
    evidence_text: str
    answer_text: str = ''
    referenced_chunks: list[dict[str, str]] = field(default_factory=list)
    router_used: str = 'agentic_discovery_only'


@dataclass
class AgentState:
    """Mutable state carried through the 2-phase orchestrator.

    Phase 1: Document selection (discovery + KG)
    Phase 2: Per-document navigation (scope_navigate_step per doc)
    Phase 3: Assembly + final verdict
    """
    # Timing
    start_time: float = field(default_factory=time.monotonic)
    step_count: int = 0

    # Phase 1: Discovery
    discovery_top_doc_ids: list[str] = field(default_factory=list)

    # Phase 1: KG document selection
    selected_docs: list[CandidateDoc] = field(default_factory=list)
    doc_id_to_name: dict[str, str] = field(default_factory=dict)
    doc_job_map: dict[str, str] = field(default_factory=dict)

    # Phase 2: Per-document navigation results
    doc_trees: dict[str, DocTreeNode] = field(default_factory=dict)  # doc_id → DocTreeNode

    # Revision state
    revision_count: int = 0
    ever_explored_doc_ids: set[str] = field(default_factory=set)
    seen_section_keys: set[str] = field(default_factory=set)  # "{doc_id}::{section_path}"

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.start_time) * 1000)
