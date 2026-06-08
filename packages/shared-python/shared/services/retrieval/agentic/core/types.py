"""Core data types for agentic retrieval.

Defines the state machine primitives: configuration, state, actions,
observations, and tool results. All types are plain dataclasses with
no business logic — they are pure data containers.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from shared.services.retrieval.agentic.core.budget import BudgetLedger


@dataclass
class AgentRunConfig:
    """Budget and limit configuration for a single agent run."""
    max_nav_steps: int = 6  # max navigation steps per document (no depth limit)
    latency_budget_ms: int = 12000
    token_budget_total: int = 40000
    planning_ratio: float = 0.5
    bootstrap_budget: int = 2000
    per_doc_min_share: int = 1500
    inventory_aware: bool = True


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
    tokens_used: int = 0


@dataclass
class DocTreeNode:
    """Unified navigation result tree for one document.

    Produced by ``navigate_step``. Captures the full
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

    def has_leaf_content(self) -> bool:
        """Check if this tree has any actual hydrated chunk content (not just outline).

        Unlike ``has_content()`` which returns True for outline-only trees,
        this method only returns True when real text/table/image chunks have
        been hydrated into leaf_content.
        """
        if self.leaf_content:
            return True
        return any(c.has_leaf_content() for c in self.children.values())

    def flatten_chunk_rows(self) -> list[dict[str, Any]]:
        """Recursively collect all hydrated chunk rows (document order)."""
        rows: list[dict[str, Any]] = []
        for chunks in self.leaf_content.values():
            rows.extend(chunks)
        for child in self.children.values():
            rows.extend(child.flatten_chunk_rows())
        return rows

    def add_leaf_chunks(self, path: str, chunks: list[dict[str, Any]]) -> None:
        """Merge chunks into a leaf path, deduplicating by (chunk_id, path)."""
        if not path or not chunks:
            return
        existing = self.leaf_content.setdefault(path, [])
        seen: set[tuple[str, str]] = {
            (str(row.get('chunk_id') or ''), path)
            for row in existing
            if row.get('chunk_id')
        }
        for chunk in chunks:
            chunk_id = str(chunk.get('chunk_id') or '')
            key = (chunk_id, path)
            if chunk_id and key in seen:
                continue
            if chunk_id:
                seen.add(key)
            existing.append(chunk)

    def reparent_leaf_content(self) -> None:
        """Move descendant leaf paths into matching child nodes.

        Only moves true descendants (prefix match).  Content whose path
        exactly equals a child key stays here — the renderer handles the
        case where a path is both a child and a leaf (section with own
        content *and* sub-sections).
        """
        for child_path, child in list(self.children.items()):
            for leaf_path in list(self.leaf_content.keys()):
                if leaf_path.startswith(child_path + ' / '):
                    child.add_leaf_chunks(leaf_path, self.leaf_content.pop(leaf_path))
            child.reparent_leaf_content()

    def collect_referenced_ids(self, *, document_name: str = '') -> list[dict[str, Any]]:
        """Extract minimal chunk references from all hydrated leaves.

        Returns deduplicated list of {chunk_id, document_id, chunk_type,
        section_path, file_path, job_id, score} for hit stats and frontend
        display. ``score`` carries the real retrieval score (discovery RRF or
        navigation confidence) so callers can surface it in results.
        """
        refs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in self.flatten_chunk_rows():
            cid = row.get('chunk_id', '')
            if cid and cid not in seen:
                seen.add(cid)
                section_path = row.get('section_path', '')
                if section_path == 'Root' and document_name:
                    section_path = document_name
                refs.append({
                    'chunk_id': cid,
                    'document_id': row.get('document_id', ''),
                    'chunk_type': row.get('chunk_type', ''),
                    'section_path': section_path,
                    'file_path': row.get('file_path', ''),
                    'job_id': row.get('job_id', ''),
                    'score': row.get('score'),  # None if no real score available
                })
        return refs

    def merge(self, other: 'DocTreeNode') -> None:
        """Additive merge for navigation results.

        Merges outline items, leaf content, children, and confidence from
        ``other`` into this node. Existing data is preserved; new data is
        added. For confidence values, the higher value wins.
        """
        existing_paths = {item['path'] for item in self.outline_items}
        for item in other.outline_items:
            if item.get('path', '') not in existing_paths:
                self.outline_items.append(item)
        for path, chunks in other.leaf_content.items():
            self.add_leaf_chunks(path, chunks)
        for path, child in other.children.items():
            if path in self.children:
                self.children[path].merge(child)
            else:
                self.children[path] = child
        for path, conf in other.confidence.items():
            self.confidence[path] = max(self.confidence.get(path, 0), conf)
        self.reparent_leaf_content()


NavAction = Literal[
    "EXPAND",
    "BACK",
    "FINISH",
    "SEARCH_IMAGES",
    "SEARCH_TABLES",
    "ERROR",
]


@dataclass
class DecisionTraceStep:
    """Uniform observe-act-result trace entry exposed to downstream agents."""

    step_index: int
    agent: str
    phase: str
    observation: dict[str, Any]
    decision: dict[str, Any]
    result: dict[str, Any]
    parent_step_index: int | None = None
    document_id: str | None = None
    document: str | None = None
    scope: str | None = None
    budget: dict[str, Any] | None = None
    elapsed_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "step_index": self.step_index,
            "agent": self.agent,
            "parent_step_index": self.parent_step_index,
            "phase": self.phase,
            "document_id": self.document_id,
            "document": self.document,
            "scope": self.scope,
            "observation": self.observation,
            "decision": self.decision,
            "result": self.result,
        }
        if self.budget is not None:
            data["budget"] = self.budget
        if self.elapsed_ms is not None:
            data["elapsed_ms"] = self.elapsed_ms
        return data


@dataclass
class NavigateStepResult:
    """Return type for navigate_step — Collector Agent model.

    Each step returns:
    - ``collect``: paths to add to the evidence collection (full hydration)
    - ``drill``: paths to explore deeper in subsequent steps
    - ``action``: one explicit action — EXPAND/BACK/FINISH/SEARCH_*/ERROR
    - ``node``: outline tree node for rendering context
    - ``reason``: LLM reasoning for trace
    - ``error_reason``: set when action is ERROR — distinguishes system
      errors from intentional FINISH so callers can decide retry vs skip.
    - ``search_assets_params``: parameters for SEARCH_IMAGES/SEARCH_TABLES
    - ``observation``: what the navigator saw before choosing the action
    - ``result_status`` / ``result_note``: executor-visible action validation
    """
    action: NavAction = "FINISH"
    collect: list[dict[str, Any]] = field(default_factory=list)
    drill: list[dict[str, Any]] = field(default_factory=list)
    back_to: str | None = None  # BACK target ancestor path (None = root)
    tools: list[str] = field(default_factory=list)
    node: DocTreeNode = field(default_factory=DocTreeNode)
    reason: str = ""
    error_reason: str | None = None
    search_assets_params: dict[str, Any] | None = None
    observation: dict[str, Any] = field(default_factory=dict)
    result_status: str = "ok"
    result_note: str | None = None

    @property
    def drill_into(self) -> str | None:
        """Single drill target path, or None."""
        return self.drill[0]["path"] if self.drill else None

    @property
    def is_terminal(self) -> bool:
        """True only for explicit terminal actions."""
        return self.action in ("FINISH", "ERROR")

    @staticmethod
    def stop(scope_path: str | None = None, *, reason: str = "") -> 'NavigateStepResult':
        return NavigateStepResult(
            action="FINISH",
            node=DocTreeNode.empty(scope_path),
            reason=reason,
        )

    @staticmethod
    def error(scope_path: str | None = None, *, reason: str = "") -> 'NavigateStepResult':
        """Return an ERROR result distinguishable from intentional FINISH."""
        return NavigateStepResult(
            action="ERROR",
            node=DocTreeNode.empty(scope_path),
            reason=f"navigation_error: {reason[:200]}" if reason else "navigation_error",
            error_reason=reason[:500] if reason else "unknown_error",
            result_status="error",
            result_note=reason[:500] if reason else "unknown_error",
        )


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
    - ``answer_text``: deprecated; always empty because KNOWHERE returns
      evidence only and downstream agents synthesize answers.
    - ``referenced_chunks``: minimal chunk references for hit stats
      and frontend display (chunk_id, document_id, chunk_type, etc.)
    - ``router_used``: routing path identifier
    - ``budget_snapshot``: final budget ledger state at run completion
    - ``stop_reason``: why the run terminated (evidence_only /
      budget / latency / max_steps / error / llm_stop)
    - ``failure_reason``: fatal retrieval failure reason, if any.
    - ``decision_trace``: per-step navigation decisions with reasons,
      exposed to downstream agents for stop/retry/modify-query decisions.
    """
    evidence_text: str
    answer_text: str = ''
    referenced_chunks: list[dict[str, str]] = field(default_factory=list)
    router_used: str = 'agentic_discovery_only'
    budget_snapshot: dict[str, Any] | None = None
    stop_reason: str = ''
    failure_reason: str = ''
    decision_trace: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AgentState:
    """Mutable state carried through the 2-phase orchestrator.

    Phase 1: Document selection (discovery + KG)
    Phase 2: Per-document navigation (navigate_step per doc)
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

    ever_explored_doc_ids: set[str] = field(default_factory=set)

    # Token budget + KG inventory
    ledger: BudgetLedger | None = None
    kg_total_chunks: int = 0
    kg_total_docs: int = 0
    explored_chunks: int = 0

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.start_time) * 1000)
