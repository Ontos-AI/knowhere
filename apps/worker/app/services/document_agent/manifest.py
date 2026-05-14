"""Stable Phase 1 shard manifest contract.

The manifest is intentionally independent from existing parser internals so it
can be produced and inspected before parser integration work begins.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


SpecialKind = Literal[
    "toc",
    "blank",
    "sparse",
    "table_heavy",
    "image_heavy",
    "landscape",
    "single_image",
    "normal",
]

PredominantKind = Literal[
    "text_dense",
    "table_heavy",
    "image_heavy",
    "mixed",
    "landscape_block",
    "toc",
    "sparse",
]


@dataclass
class SpecialPage:
    page: int
    kind: SpecialKind
    confidence: float
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ShardSignal:
    page_start: int
    page_end: int
    page_offset: int
    predominant_kind: PredominantKind
    special_pages: list[SpecialPage] = field(default_factory=list)
    estimated_difficulty: str | None = None
    parser_hint: str | None = None
    cut_rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["special_pages"] = [page.to_dict() for page in self.special_pages]
        return data


@dataclass
class GlobalSignals:
    has_toc: bool
    toc_pages: list[int]
    landscape_ratio: float
    table_page_ratio: float
    image_page_ratio: float
    sample_size: int
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ShardManifest:
    job_id: str
    file_uri: str
    file_sha: str
    page_count: int
    shard_count: int
    shards: list[ShardSignal]
    global_signals: GlobalSignals
    decision_log_ref: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0"

    def validate(self) -> None:
        """Enforce the downstream coverage contract."""
        if self.page_count < 0:
            raise ValueError("page_count must be non-negative")
        if self.shard_count != len(self.shards):
            raise ValueError("shard_count must match shards length")
        if self.page_count == 0:
            if self.shards:
                raise ValueError("empty documents cannot contain shards")
            return

        expected_start = 1
        for shard in self.shards:
            if shard.page_start != expected_start:
                raise ValueError(
                    f"non-contiguous shard coverage at page {expected_start}: "
                    f"got start={shard.page_start}"
                )
            if shard.page_end < shard.page_start:
                raise ValueError(
                    f"invalid shard range {shard.page_start}-{shard.page_end}"
                )
            if shard.page_offset != shard.page_start - 1:
                raise ValueError(
                    f"invalid page_offset for shard {shard.page_start}-{shard.page_end}"
                )
            expected_start = shard.page_end + 1

        if expected_start != self.page_count + 1:
            raise ValueError(
                f"shards must cover 1..{self.page_count}, stopped at {expected_start - 1}"
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "version": self.version,
            "job_id": self.job_id,
            "file_uri": self.file_uri,
            "file_sha": self.file_sha,
            "page_count": self.page_count,
            "shard_count": self.shard_count,
            "shards": [shard.to_dict() for shard in self.shards],
            "global_signals": self.global_signals.to_dict(),
            "decision_log_ref": self.decision_log_ref,
            "created_at": self.created_at.isoformat(),
        }
