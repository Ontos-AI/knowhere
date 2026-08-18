"""Calibration result types.

Phase-1 produces only what Phase-2 cannot recompute: ``status``, per-regime
numbering ``kind`` + candidate ``offset`` (plus the anchor ``samples`` already
confirmed by vision), and one short ``notes`` reason. ``segments`` /
``no_toc_entry_indices`` / ``offset_status`` / per-regime ``notes`` are Phase-2
outputs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Failure classes recorded on ``CalibrationResult.failure_kind``, one per
# failure exit of Phase-1.
FAILURE_NO_OFFSET = "no_offset"
FAILURE_PAGE_COUNT_MISSING = "page_count_missing"
FAILURE_TOC_EMPTY = "toc_empty"


@dataclass
class CalibrationSample:
    """A printed→physical anchor confirmed with ``inspect.pages``."""

    title: str
    physical: int | None = None


@dataclass
class CalibrationSegment:
    """A contiguous leaf range that fully completed Phase-2 for one offset."""

    offset: int
    leaf_start: int
    leaf_end: int
    entry_indices: list[int] = field(default_factory=list)
    status: str = "ok"


@dataclass
class CalibrationRegime:
    kind: str
    offset: int | None = None
    # Optional membership override; empty → Phase-2 matches leaves by ``kind``.
    entry_indices: list[int] = field(default_factory=list)
    samples: list[CalibrationSample] = field(default_factory=list)
    # Phase-2 outputs below; Phase-1 never fills these.
    offset_status: str = "failed"
    segments: list[CalibrationSegment] = field(default_factory=list)
    no_toc_entry_indices: list[int] = field(default_factory=list)
    notes: str = ""


@dataclass
class CalibrationResult:
    status: str
    regimes: list[CalibrationRegime] = field(default_factory=list)
    offset: int | None = None
    offset_status: str = "failed"
    tool_calls: int = 0
    notes: str = ""
    # Empty on success; otherwise one of the FAILURE_* constants.
    failure_kind: str = ""
    region_index: int | None = None
    # Debug-only trail: one entry per scanned probe title.
    scans: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


