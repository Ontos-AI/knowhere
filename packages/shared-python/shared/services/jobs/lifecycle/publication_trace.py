"""Measurement-only trace for one publication transaction attempt.

The transaction owner creates and finishes the trace. Shared publication modules
receive it explicitly and record only named stages and numeric observations.
"""

from __future__ import annotations

import re
import time
from hashlib import sha256
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Literal

from loguru import logger

from shared.core.logging import LogEvent


_OPAQUE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SCOPE_FINGERPRINT = re.compile(r"^scope-[a-f0-9]{16,64}$")
_STAGES = frozenset(
    {
        "unattributed",
        "pool_checkout_wait",
        "existing_document_lock_wait",
        "document_resolution",
        "revision_update",
        "result_binding",
        "sections_prepare",
        "sections_persist",
        "chunks_prepare",
        "chunks_persist",
        "serving_index_prepare",
        "map_units_persist",
        "tokens_persist",
        "statistics_prepare",
        "statistics_persist",
        "serving_manifest_prepare",
        "serving_manifest_persist",
        "graph_prepare",
        "graph_persist",
        "namespace_generation_lock_wait",
        "namespace_snapshot_prepare",
        "namespace_snapshot_persist",
        "commit",
        "rollback",
    }
)
_COUNTERS = frozenset(
    {
        "input_text_chunks",
        "input_image_chunks",
        "input_table_chunks",
        "input_page_chunks",
        "sections",
        "chunks",
        "map_units",
        "tokens",
        "graph_edges",
        "namespace_active_documents",
        "manifest_compressed_bytes",
        "manifest_uncompressed_bytes",
        "snapshot_compressed_bytes",
        "snapshot_uncompressed_bytes",
    }
)
_OWNERS = frozenset({"sync", "async"})
_OUTCOMES = frozenset({"success", "rollback"})


class PublicationTrace:
    """Aggregate safe measurements for exactly one publication attempt."""

    def __init__(
        self,
        *,
        attempt_ref: str,
        owner: Literal["sync", "async"],
        job_id: str,
        job_result_id: str | None = None,
        document_id: str | None = None,
        scope_fingerprint: str | None = None,
        job_type: Literal["document_ingestion", "demo_materialization"] | None = None,
        parse_track: Literal["chunk", "page_memory"] | None = None,
    ) -> None:
        self._attempt_ref = self._validate_identifier(attempt_ref)
        self._job_id = self._validate_identifier(job_id)
        self._job_result_id = self._validate_optional_identifier(job_result_id)
        self._document_id = self._validate_optional_identifier(document_id)
        if scope_fingerprint is not None and not _SCOPE_FINGERPRINT.fullmatch(
            scope_fingerprint
        ):
            raise ValueError("scope fingerprint must be an opaque digest")
        self._scope_fingerprint = scope_fingerprint
        if owner not in _OWNERS:
            raise ValueError("publication owner must be sync or async")
        if job_type not in (None, "document_ingestion", "demo_materialization"):
            raise ValueError("unsupported publication job type")
        if parse_track not in (None, "chunk", "page_memory"):
            raise ValueError("unsupported publication parse track")
        self._owner = owner
        self._job_type = job_type
        self._parse_track = parse_track
        self._started_at = time.perf_counter()
        self._stage_stack: list[str] = []
        self._stage_duration_ms: dict[str, float] = {}
        self._stage_sql: dict[str, dict[str, float | int]] = {}
        self._counters: dict[str, int] = {}
        self._sql_statement_count = 0
        self._sql_batch_count = 0
        self._sql_duration_ms = 0.0
        self._sql_max_statement_ms = 0.0
        self._finished = False

    @classmethod
    def start(
        cls,
        *,
        attempt_ref: str,
        owner: Literal["sync", "async"],
        job_id: str,
        job_result_id: str | None = None,
        document_id: str | None = None,
        scope_fingerprint: str | None = None,
        job_type: Literal["document_ingestion", "demo_materialization"] | None = None,
        parse_track: Literal["chunk", "page_memory"] | None = None,
    ) -> PublicationTrace:
        """Start a trace at the transaction owner boundary."""
        return cls(
            attempt_ref=attempt_ref,
            owner=owner,
            job_id=job_id,
            job_result_id=job_result_id,
            document_id=document_id,
            scope_fingerprint=scope_fingerprint,
            job_type=job_type,
            parse_track=parse_track,
        )

    @property
    def is_finished(self) -> bool:
        """Return whether a terminal event has already been emitted."""
        return self._finished

    @property
    def active_stage(self) -> str | None:
        """Return the innermost stage for connection-local SQL attribution."""
        return self._stage_stack[-1] if self._stage_stack else None

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Measure one named stage, including its SQL statements."""
        self._validate_active()
        self._validate_stage(name)
        self._stage_stack.append(name)
        started_at = time.perf_counter()
        try:
            yield
        finally:
            self.record_stage(name, (time.perf_counter() - started_at) * 1000)
            self._stage_stack.pop()

    def record_stage(self, name: str, duration_ms: float) -> None:
        """Add a measured stage duration supplied by an external boundary."""
        self._validate_active()
        self._validate_stage(name)
        duration = self._validate_duration(duration_ms)
        self._stage_duration_ms[name] = self._stage_duration_ms.get(name, 0.0) + duration

    def record_count(self, name: str, count: int) -> None:
        """Set one safe numeric publication count."""
        self._validate_active()
        if name not in _COUNTERS:
            raise ValueError("unsupported publication counter")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("publication counter must be a nonnegative integer")
        self._counters[name] = count

    def bind_result_id(self, job_result_id: str) -> None:
        """Bind the result ID after the existing result upsert completes."""
        self._validate_active()
        self._job_result_id = self._validate_identifier(job_result_id)

    def bind_document_id(self, document_id: str) -> None:
        """Bind the resolved document's opaque identifier."""
        self._validate_active()
        self._document_id = self._validate_identifier(document_id)

    def bind_scope(self, *, user_id: str, namespace: str) -> None:
        """Retain only a stable digest of the resolved publication scope."""
        self._validate_active()
        raw_scope = f"{user_id}\0{namespace}"
        self._scope_fingerprint = "scope-" + sha256(
            raw_scope.encode("utf-8")
        ).hexdigest()[:16]

    def bind_job_type(self, job_type: str) -> None:
        """Bind a known job category without retaining arbitrary metadata."""
        self._validate_active()
        if job_type not in ("document_ingestion", "demo_materialization"):
            raise ValueError("unsupported publication job type")
        self._job_type = job_type

    def bind_parse_track(self, parse_track: str) -> None:
        """Bind a known parser track without retaining arbitrary metadata."""
        self._validate_active()
        if parse_track not in ("chunk", "page_memory"):
            raise ValueError("unsupported publication parse track")
        self._parse_track = parse_track

    def record_sql(
        self,
        stage: str | None,
        statement_count: int,
        total_sql_ms: float,
        max_statement_ms: float,
        *,
        batch_count: int | None = None,
    ) -> None:
        """Accept cumulative SQL aggregates for one stage without SQL payloads."""
        self._validate_active()
        if stage is not None:
            self._validate_stage(stage)
        if (
            isinstance(statement_count, bool)
            or not isinstance(statement_count, int)
            or statement_count < 1
        ):
            raise ValueError("SQL statement count must be a positive integer")
        total_duration = self._validate_duration(total_sql_ms)
        max_duration = self._validate_duration(max_statement_ms)
        if max_duration > total_duration:
            raise ValueError("maximum SQL statement duration exceeds total duration")
        if batch_count is not None and (
            isinstance(batch_count, bool)
            or not isinstance(batch_count, int)
            or batch_count < 0
        ):
            raise ValueError("SQL batch count must be a nonnegative integer")
        attributed_stage = stage or self.active_stage
        if attributed_stage is None:
            attributed_stage = "unattributed"
        stage_sql = self._stage_sql.setdefault(
            attributed_stage,
            {
                "statement_count": 0,
                "batch_count": 0,
                "total_sql_ms": 0.0,
                "max_statement_ms": 0.0,
            },
        )
        if (
            statement_count < stage_sql["statement_count"]
            or (batch_count is not None and batch_count < stage_sql["batch_count"])
            or total_duration < stage_sql["total_sql_ms"]
            or max_duration < stage_sql["max_statement_ms"]
        ):
            raise ValueError("SQL stage aggregates must be cumulative")
        self._sql_statement_count += statement_count - int(stage_sql["statement_count"])
        if batch_count is not None:
            self._sql_batch_count += batch_count - int(stage_sql["batch_count"])
        self._sql_duration_ms += total_duration - float(stage_sql["total_sql_ms"])
        self._sql_max_statement_ms = max(self._sql_max_statement_ms, max_duration)
        stage_sql["statement_count"] = statement_count
        if batch_count is not None:
            stage_sql["batch_count"] = batch_count
        stage_sql["total_sql_ms"] = total_duration
        stage_sql["max_statement_ms"] = max_duration

    def finish(self, *, outcome: Literal["success", "rollback"]) -> Mapping[str, object]:
        """Emit exactly one structured terminal event and return its safe payload."""
        self._validate_active()
        if outcome not in _OUTCOMES:
            raise ValueError("publication outcome must be success or rollback")
        if self._stage_stack:
            raise RuntimeError("cannot finish publication trace inside a stage")
        self._finished = True
        payload: dict[str, object] = {
            "attempt_ref": self._attempt_ref,
            "owner": self._owner,
            "job_id": self._job_id,
            "job_result_id": self._job_result_id,
            "document_id": self._document_id,
            "scope_fingerprint": self._scope_fingerprint,
            "job_type": self._job_type,
            "parse_track": self._parse_track,
            "outcome": outcome,
            "duration_ms": round((time.perf_counter() - self._started_at) * 1000, 3),
            "counts": dict(self._counters),
            "stages": {
                name: {
                    "duration_ms": round(self._stage_duration_ms.get(name, 0.0), 3),
                    "statement_count": int(
                        self._stage_sql.get(name, {}).get("statement_count", 0)
                    ),
                    "total_sql_ms": round(
                        float(self._stage_sql.get(name, {}).get("total_sql_ms", 0.0)),
                        3,
                    ),
                    "max_statement_ms": round(
                        float(self._stage_sql.get(name, {}).get("max_statement_ms", 0.0)),
                        3,
                    ),
                }
                for name in sorted(self._stage_duration_ms.keys() | self._stage_sql.keys())
            },
            "sql": {
                "statement_count": self._sql_statement_count,
                "batch_count": self._sql_batch_count,
                "total_sql_ms": round(self._sql_duration_ms, 3),
                "max_statement_ms": round(self._sql_max_statement_ms, 3),
            },
        }
        logger.bind(
            event=LogEvent.PUBLICATION_TRACE_TERMINAL.value,
            publication_trace=payload,
        ).info(
            "Publication transaction completed"
        )
        return payload

    def _validate_active(self) -> None:
        if self._finished:
            raise RuntimeError("publication trace is already finished")

    @staticmethod
    def _validate_identifier(value: str) -> str:
        if not isinstance(value, str) or not _OPAQUE_IDENTIFIER.fullmatch(value):
            raise ValueError("publication identifier must be opaque ASCII")
        return value

    @classmethod
    def _validate_optional_identifier(cls, value: str | None) -> str | None:
        return None if value is None else cls._validate_identifier(value)

    @staticmethod
    def _validate_stage(name: str) -> None:
        if name not in _STAGES:
            raise ValueError("unsupported publication stage")

    @staticmethod
    def _validate_duration(duration_ms: float) -> float:
        import math

        if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
            raise ValueError("duration must be a nonnegative finite number")
        duration = float(duration_ms)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("duration must be a nonnegative finite number")
        return duration
