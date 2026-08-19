"""Calibration SubAgent result types.

The ``calibration.submit`` payload carries only what Phase-2 cannot recompute:
``status``, per-regime numbering ``kind`` + candidate ``offset`` (plus the
anchor ``samples`` already confirmed by vision), and one short ``notes`` reason.
``segments`` / ``no_toc_entry_indices`` / ``offset_status`` / per-regime
``notes`` / ``tool_calls`` / ``region_index`` are Phase-2 or harness outputs and
are never read back from a submit payload.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Failure classes recorded on ``CalibrationResult.failure_kind``, one per
# failure exit of the ReAct loop. ``INVALID_JSON`` means the decision payload
# did not parse (history carries completion_tokens vs max_tokens so truncation
# can be diagnosed offline); the rest mean the episode ran without an offset.
FAILURE_INVALID_JSON = "invalid_json"
FAILURE_LLM_ERROR = "llm_error"
FAILURE_MODEL_MISSING = "model_missing"
FAILURE_BUDGET_EXHAUSTED = "budget_exhausted"
FAILURE_MAX_ROUNDS = "max_rounds"
FAILURE_NO_OFFSET = "no_offset"
FAILURE_TOC_EMPTY = "toc_empty"


@dataclass
class CalibrationSample:
    """A printed→physical anchor the agent confirmed with ``inspect.pages``."""

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
    entry_indices: list[int] = field(default_factory=list)
    samples: list[CalibrationSample] = field(default_factory=list)
    # Phase-2 outputs below; never parsed from the agent submit payload.
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
    # Empty on success; otherwise one of the FAILURE_* constants, so a submit
    # that never parsed is never read as "this document has no offset".
    failure_kind: str = ""
    region_index: int | None = None
    # Debug-only trail from the ReAct loop (not part of submit schema).
    history_tail: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _as_int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        parsed = _as_optional_int(item)
        if parsed is not None:
            out.append(parsed)
    return out


def calibration_result_from_dict(data: dict[str, Any]) -> CalibrationResult:
    """Parse the minimal submit payload; ignore anything Phase-2 recomputes."""
    regimes: list[CalibrationRegime] = []
    for raw in data.get("regimes") or []:
        if not isinstance(raw, dict):
            continue
        samples = [
            CalibrationSample(
                title=str(s.get("title") or ""),
                physical=_as_optional_int(s.get("physical")),
            )
            for s in (raw.get("samples") or [])
            if isinstance(s, dict)
        ]
        regimes.append(
            CalibrationRegime(
                kind=str(raw.get("kind") or "other"),
                offset=_as_optional_int(raw.get("offset")),
                entry_indices=_as_int_list(raw.get("entry_indices")),
                samples=samples,
            )
        )
    return CalibrationResult(
        status=str(data.get("status") or "failed"),
        regimes=regimes,
        notes=str(data.get("notes") or ""),
        failure_kind=str(data.get("failure_kind") or ""),
    )
