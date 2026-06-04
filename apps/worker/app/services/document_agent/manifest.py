"""Contracts for the hierarchy-first document profile agent."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


PageKind = Literal["normal", "table_heavy", "image_heavy", "low_content", "landscape"]

ReflexionAction = Literal["tool_call", "verdict_now"]
VerdictStatus = Literal["success", "abort"]


@dataclass
class PageFeature:
    page: int
    raw_text_length: int
    text_density: float
    image_coverage: float
    image_count: int
    table_count: int
    drawings_count: int
    orientation: Literal["portrait", "landscape"]
    width: float
    height: float
    is_blank_like: bool
    text_lines_preview: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PageLabel:
    page: int
    kind: PageKind
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DocumentProfile:
    is_scanned: bool
    category: str
    category_rationale: str = ""
    language: str = "unknown"
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentVerdict:
    status: VerdictStatus
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReflexionDecision:
    action: ReflexionAction
    rationale: str
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    verdict: AgentVerdict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "rationale": self.rationale,
            "tool_name": self.tool_name,
            "tool_args": dict(self.tool_args),
            "verdict": self.verdict.to_dict() if self.verdict else None,
        }


@dataclass
class TocCandidate:
    title: str
    normalized_title: str
    source_page: int
    line_index: int
    numbering: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TocAnchorPage:
    """A candidate TOC start page identified by keyword scan, pending VLM confirmation."""

    page: int  # 1-based page number
    png_path: str  # local PNG path for VLM inspection
    source: Literal["page_label", "text_scan", "visual_scan"]  # how this anchor was discovered

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TocResult:
    toc_pages: list[int] = field(default_factory=list)
    candidates: list[TocCandidate] = field(default_factory=list)
    method: Literal["toc_marker", "vlm_progressive", "vlm_batch", "visual_scan", "none"] = "none"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["candidates"] = [candidate.to_dict() for candidate in self.candidates]
        return data


@dataclass
class H1Candidate:
    title: str
    page: int
    confidence: float
    matched_line: str
    source: Literal["toc_exact_top", "toc_fuzzy_top", "heading_grep", "toc_grep", "h2_refine", "none"]
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class H1BoundaryResult:
    h1_candidates: list[H1Candidate] = field(default_factory=list)
    method: Literal["toc_grep", "heading_grep", "none"] = "none"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["h1_candidates"] = [candidate.to_dict() for candidate in self.h1_candidates]
        return data


@dataclass
class ValidationReport:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Shard:
    shard_index: int
    page_start: int
    page_end: int
    page_offset: int
    anchor_type: Literal["h1_boundary", "blank_separator", "forced_max_size"]
    anchor_evidence: str
    confidence: float
    split_depth: int = 1        # 1=H1 cut, 2=H2 cut, etc.
    is_continuation: bool = False  # True for continuation shards that don't contain parent heading

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ShardPlan:
    enabled: bool
    reason: Literal[
        "too_large",
        "not_needed",
        "parser_stability",
        "hierarchy_isolation",
        "llm_boundary_decision",
    ]
    shards: list[Shard] = field(default_factory=list)
    validation: ValidationReport = field(
        default_factory=lambda: ValidationReport(valid=True)
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "reason": self.reason,
            "shards": [shard.to_dict() for shard in self.shards],
            "validation": self.validation.to_dict(),
        }


@dataclass
class PageAnatomyMap:
    job_id: str
    file_path: str
    page_count: int
    page_features: list[PageFeature]
    page_labels: list[PageLabel]
    toc_result: TocResult
    h1_result: H1BoundaryResult
    shard_plan: ShardPlan
    document_profile: DocumentProfile | None = None
    toc_hierarchies: list[dict[str, Any]] | None = None
    global_signals: dict[str, Any] = field(default_factory=dict)
    trace_summary: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "job_id": self.job_id,
            "file_path": self.file_path,
            "page_count": self.page_count,
            "page_features": [feature.to_dict() for feature in self.page_features],
            "page_labels": [label.to_dict() for label in self.page_labels],
            "toc_result": self.toc_result.to_dict(),
            "h1_result": self.h1_result.to_dict(),
            "shard_plan": self.shard_plan.to_dict(),
            "document_profile": self.document_profile.to_dict()
            if self.document_profile
            else None,
            "toc_hierarchies": self.toc_hierarchies,
            "global_signals": dict(self.global_signals),
            "trace_summary": dict(self.trace_summary),
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class ToolResult:
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    error: str | None = None
    tokens_used: int = 0
    input_summary: dict[str, Any] | None = None
    output_summary: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    debug: dict[str, Any] | None = None

@dataclass
class ToolContext:
    pdf_path: str
    job_id: str
    blackboard: Any
    budget: Any
    trace: Any
    output_dir: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
