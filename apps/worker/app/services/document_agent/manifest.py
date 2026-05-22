"""Contracts for the hierarchy-first document profile agent."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


PageKind = Literal[
    "cover",
    "toc",
    "preface",
    "normal",
    "chapter_start",
    "section_start",
    "table_heavy",
    "image_heavy",
    "single_image",
    "blank",
    "separator",
    "appendix",
    "scan_like",
    "landscape",
    "sparse",
]

BoundaryCandidateKind = Literal[
    "h1",
    "toc",
    "blank",
    "sparse",
    "separator",
]


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
class TocCandidate:
    title: str
    normalized_title: str
    source_page: int
    line_index: int
    numbering: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TocResult:
    toc_pages: list[int] = field(default_factory=list)
    candidates: list[TocCandidate] = field(default_factory=list)
    method: Literal["toc_marker", "none"] = "none"
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
    source: Literal["toc_exact_top", "toc_fuzzy_top", "heading_grep", "none"]
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
class BoundaryHint:
    page: int
    anchor_type: Literal["h1_boundary", "blank_separator", "separator", "forced_max_size"]
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BoundaryCandidate:
    page: int
    kind: BoundaryCandidateKind
    priority: int
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HierarchyAssistPlan:
    exclude_pages_from_title_candidates: list[int] = field(default_factory=list)
    prefer_h1_start_pages: list[H1Candidate] = field(default_factory=list)
    suppress_title_pages: list[int] = field(default_factory=list)
    section_boundary_hints: list[BoundaryHint] = field(default_factory=list)
    smart_parse_recommendation: Literal["off", "normal", "aggressive"] = "normal"
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "exclude_pages_from_title_candidates": list(
                self.exclude_pages_from_title_candidates
            ),
            "prefer_h1_start_pages": [
                candidate.to_dict() for candidate in self.prefer_h1_start_pages
            ],
            "suppress_title_pages": list(self.suppress_title_pages),
            "section_boundary_hints": [
                hint.to_dict() for hint in self.section_boundary_hints
            ],
            "smart_parse_recommendation": self.smart_parse_recommendation,
            "rationale": self.rationale,
        }


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
class PagePlanEntry:
    page_index: int
    strategy: Literal["vlm_detail", "vlm_lite", "text_only", "skip_tagging"]
    expected_kind: str
    rationale: str
    estimated_cost_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PageProcessingPlan:
    entries: list[PagePlanEntry] = field(default_factory=list)
    global_strategy_summary: dict[str, int] = field(default_factory=dict)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "global_strategy_summary": dict(self.global_strategy_summary),
            "rationale": self.rationale,
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
    hierarchy_assist: HierarchyAssistPlan
    shard_plan: ShardPlan
    boundary_candidates: list[BoundaryCandidate] = field(default_factory=list)
    page_processing_plan: PageProcessingPlan | None = None
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
            "hierarchy_assist": self.hierarchy_assist.to_dict(),
            "shard_plan": self.shard_plan.to_dict(),
            "boundary_candidates": [
                candidate.to_dict() for candidate in self.boundary_candidates
            ],
            "page_processing_plan": (
                self.page_processing_plan.to_dict()
                if self.page_processing_plan is not None
                else None
            ),
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
