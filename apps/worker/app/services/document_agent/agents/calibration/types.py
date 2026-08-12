"""Calibration SubAgent result types."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CalibrationSample:
    title: str
    printed_label: str | int | None = None
    physical: int | None = None
    method: str | None = None


@dataclass
class CalibrationPosterior:
    title: str
    expected_physical: int | None = None
    confirmed: bool | None = None
    method: str | None = None


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
    offset_status: str = "failed"
    entry_indices: list[int] = field(default_factory=list)
    samples: list[CalibrationSample] = field(default_factory=list)
    posterior: list[CalibrationPosterior] = field(default_factory=list)
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
    regimes: list[CalibrationRegime] = []
    for raw in data.get("regimes") or []:
        if not isinstance(raw, dict):
            continue
        samples = [
            CalibrationSample(
                title=str(s.get("title") or ""),
                printed_label=s.get("printed_label"),
                physical=_as_optional_int(s.get("physical")),
                method=s.get("method") if isinstance(s.get("method"), str) else None,
            )
            for s in (raw.get("samples") or [])
            if isinstance(s, dict)
        ]
        posterior = [
            CalibrationPosterior(
                title=str(p.get("title") or ""),
                expected_physical=_as_optional_int(p.get("expected_physical")),
                confirmed=p.get("confirmed") if isinstance(p.get("confirmed"), bool) else None,
                method=p.get("method") if isinstance(p.get("method"), str) else None,
            )
            for p in (raw.get("posterior") or [])
            if isinstance(p, dict)
        ]
        segments = [
            CalibrationSegment(
                offset=_as_optional_int(seg.get("offset")) or 0,
                leaf_start=_as_optional_int(seg.get("leaf_start")) or 0,
                leaf_end=_as_optional_int(seg.get("leaf_end")) or 0,
                entry_indices=_as_int_list(seg.get("entry_indices")),
                status=str(seg.get("status") or "ok"),
            )
            for seg in (raw.get("segments") or [])
            if isinstance(seg, dict) and _as_optional_int(seg.get("offset")) is not None
        ]
        regimes.append(
            CalibrationRegime(
                kind=str(raw.get("kind") or "other"),
                offset=_as_optional_int(raw.get("offset")),
                offset_status=str(raw.get("offset_status") or "failed"),
                entry_indices=_as_int_list(raw.get("entry_indices")),
                samples=samples,
                posterior=posterior,
                segments=segments,
                no_toc_entry_indices=_as_int_list(raw.get("no_toc_entry_indices")),
                notes=str(raw.get("notes") or ""),
            )
        )
    return CalibrationResult(
        status=str(data.get("status") or "failed"),
        regimes=regimes,
        offset=_as_optional_int(data.get("offset")),
        offset_status=str(data.get("offset_status") or "failed"),
        tool_calls=_as_optional_int(data.get("tool_calls")) or 0,
        notes=str(data.get("notes") or ""),
        region_index=_as_optional_int(data.get("region_index")),
    )
