"""Read-only, open-loop classic retrieval probes during publication load."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scripts.publication_benchmark.publication_execution import database_url_for_owner


class RetrievalProbeError(RuntimeError):
    """A probe cannot establish its read-only or semantic contract."""


@dataclass(frozen=True)
class RetrievalProbeSpec:
    user_id: str
    namespace: str
    query: str
    protected_document_ids: tuple[str, ...]
    top_k: int = 10

    @classmethod
    def from_path(cls, path: Path) -> "RetrievalProbeSpec":
        payload: Mapping[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        document_ids: tuple[str, ...] = tuple(
            str(value).strip() for value in payload["protected_document_ids"]
        )
        if (
            not document_ids
            or any(not value for value in document_ids)
            or len(set(document_ids)) != len(document_ids)
        ):
            raise RetrievalProbeError(
                "protected_document_ids must be distinct and nonempty"
            )
        query: str = str(payload["query"]).strip()
        if not query:
            raise RetrievalProbeError("query must be nonempty")
        top_k: int = int(payload.get("top_k", 10))
        if top_k < 1:
            raise RetrievalProbeError("top_k must be positive")
        user_id: str = str(payload["user_id"]).strip()
        namespace: str = str(payload["namespace"]).strip()
        if not user_id or not namespace:
            raise RetrievalProbeError("user_id and namespace must be nonempty")
        return cls(
            user_id=user_id,
            namespace=namespace,
            query=query,
            protected_document_ids=document_ids,
            top_k=top_k,
        )


@dataclass(frozen=True)
class RetrievalProbeSample:
    scheduled_offset_ms: float
    arrival_lag_ms: float
    duration_ms: float
    result_digest: str | None
    revision_digest: str | None
    result_count: int
    error: str | None = None
    actual_start_offset_ms: float | None = None
    actual_end_offset_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheduled_offset_ms": round(self.scheduled_offset_ms, 2),
            "arrival_lag_ms": round(self.arrival_lag_ms, 2),
            "duration_ms": round(self.duration_ms, 2),
            "result_digest": self.result_digest,
            "revision_digest": self.revision_digest,
            "result_count": self.result_count,
            "error": self.error,
            "actual_start_offset_ms": self.actual_start_offset_ms,
            "actual_end_offset_ms": self.actual_end_offset_ms,
        }


def probe_samples_cover_publication(
    samples: Sequence[RetrievalProbeSample],
    *,
    probe_origin: float,
    publication_started_at: float,
    publication_ended_at: float,
) -> bool:
    """Require actual requests before publication starts and through its end."""
    starts = [
        sample.actual_start_offset_ms
        for sample in samples
        if sample.actual_start_offset_ms is not None
    ]
    ends = [
        sample.actual_end_offset_ms
        for sample in samples
        if sample.actual_end_offset_ms is not None
    ]
    if len(starts) != len(samples) or len(ends) != len(samples) or not samples:
        return False
    return (
        probe_origin + min(starts) / 1000 <= publication_started_at
        and probe_origin + max(ends) / 1000 >= publication_ended_at
    )


def digest_json(value: Any) -> str:
    """Return a redacted, deterministic digest for a semantic result."""
    canonical: str = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluate_probe_samples(
    samples: Sequence[RetrievalProbeSample],
    *,
    expected_result_digest: str | None = None,
    expected_revision_digest: str | None = None,
) -> dict[str, Any]:
    """Require every probe to return one coherent, unchanged result."""
    if not samples:
        return {"status": "fail", "failures": ["no probes completed"]}
    result_digests: set[str] = {
        value for sample in samples if (value := sample.result_digest) is not None
    }
    revision_digests: set[str] = {
        value for sample in samples if (value := sample.revision_digest) is not None
    }
    failures: list[str] = [
        f"probe {index}: {sample.error}"
        for index, sample in enumerate(samples)
        if sample.error is not None
    ]
    if any(sample.result_count == 0 for sample in samples):
        failures.append("one or more probes returned no protected evidence")
    if len(result_digests) != 1:
        failures.append("retrieval result digests differ across probes")
    if len(revision_digests) != 1:
        failures.append("protected revision digests differ across probes")
    if expected_result_digest and result_digests != {expected_result_digest}:
        failures.append("retrieval result differs from expected quiet baseline")
    if expected_revision_digest and revision_digests != {expected_revision_digest}:
        failures.append("protected revision set differs from expected quiet baseline")
    return {
        "status": "pass" if not failures else "fail",
        "failures": failures,
        "result_digest": next(iter(result_digests))
        if len(result_digests) == 1
        else None,
        "revision_digest": (
            next(iter(revision_digests)) if len(revision_digests) == 1 else None
        ),
    }


def evaluate_interference_pair(
    *, baseline_report: Mapping[str, Any], candidate_report: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare equivalent publication-plus-probe batches at the 10% p95 gate."""
    failures: list[str] = []
    for field in (
        "spec_digest",
        "rate_per_second",
        "duration_seconds",
        "template_digest",
        "host_id",
        "postgres_settings_digest",
        "clone_source_digest",
        "owner",
        "concurrency",
    ):
        if baseline_report.get(field) != candidate_report.get(field):
            failures.append(f"baseline and candidate differ on {field}")
    if baseline_report.get("strategy") != "baseline":
        failures.append("reference report is not baseline strategy")
    if candidate_report.get("strategy") != "candidate":
        failures.append("comparison report is not candidate strategy")
    if baseline_report.get("gate", {}).get("status") != "pass":
        failures.append("baseline probe gate failed")
    if candidate_report.get("gate", {}).get("status") != "pass":
        failures.append("candidate probe gate failed")
    baseline_p95 = float(baseline_report.get("p95_duration_ms") or 0)
    candidate_p95 = float(candidate_report.get("p95_duration_ms") or 0)
    if baseline_p95 <= 0:
        failures.append("baseline p95 must be positive")
    elif candidate_p95 > baseline_p95 * 1.10:
        failures.append("candidate retrieval p95 exceeds 110 percent of baseline")
    return {"status": "pass" if not failures else "fail", "failures": failures}


async def execute_retrieval_probe(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    spec: RetrievalProbeSpec,
) -> tuple[str, str, int]:
    """Run production discovery, rank, and hydration in one read-only snapshot."""
    from shared.services.retrieval.document_scope import DocumentScope
    from shared.services.retrieval.execution.revision_pins import capture_revision_pins
    from shared.services.retrieval.hydration.result_assembly import (
        assemble_retrieval_results,
    )
    from shared.services.retrieval.search.map_unit_discovery import map_unit_discovery
    from shared.services.retrieval.search.ranking import rank_retrieval_candidates

    document_scope = DocumentScope(include=frozenset(spec.protected_document_ids))
    async with session_factory() as session:
        await session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        )
        pins = await capture_revision_pins(
            session, user_id=spec.user_id, namespace=spec.namespace
        )
        protected_pins: dict[str, str] = {
            document_id: pins[document_id]
            for document_id in spec.protected_document_ids
            if document_id in pins
        }
        if len(protected_pins) != len(spec.protected_document_ids):
            raise RetrievalProbeError("protected document has no active revision")
        discovery = await map_unit_discovery(
            session,
            user_id=spec.user_id,
            namespace=spec.namespace,
            query=spec.query,
            top_k=spec.top_k,
            exclude_document_ids=[],
            exclude_sections=[],
            document_scope=document_scope,
            revision_pins=protected_pins,
            publish_index_readiness=False,
        )
        ranked = await rank_retrieval_candidates(
            session,
            user_id=spec.user_id,
            namespace=spec.namespace,
            discovery_rows=list(discovery.payload.get("fused_rows") or []),
            routed_rows=[],
            top_k=spec.top_k,
            revision_pins=protected_pins,
        )
        results = await assemble_retrieval_results(
            db=session,
            rows=ranked,
            exclude_document_ids=[],
            exclude_sections=[],
            document_scope=document_scope,
            revision_pins=protected_pins,
        )
        result_identity: list[tuple[str, str, str, str]] = []
        for result in results:
            document_id = str(result.get("document_id") or "")
            revision_id = str(result.get("job_result_id") or "")
            if protected_pins.get(document_id) != revision_id:
                raise RetrievalProbeError("result escaped its protected revision pin")
            result_identity.append(
                (
                    document_id,
                    revision_id,
                    str(result.get("chunk_id") or ""),
                    digest_json(str(result.get("content") or "")),
                )
            )
        await session.rollback()
    return (
        digest_json(result_identity),
        digest_json(sorted(protected_pins.items())),
        len(results),
    )


async def run_open_loop_probes(
    *,
    database_url: str,
    spec: RetrievalProbeSpec,
    rate_per_second: int,
    duration_seconds: float,
    timeout_seconds: float = 20.0,
    stop_event: Event | None = None,
    started_event: Event | None = None,
    origin_capture: list[float] | None = None,
) -> list[RetrievalProbeSample]:
    """Schedule independent requests by wall clock through a batch's completion."""
    if rate_per_second not in (1, 3) or duration_seconds <= 0:
        raise RetrievalProbeError(
            "rate must be 1 or 3 per second and duration positive"
        )
    async_database_url = database_url_for_owner(database_url, owner="async")
    engine = create_async_engine(async_database_url, pool_size=10, max_overflow=0)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    origin = time.monotonic()
    if origin_capture is not None:
        origin_capture.append(origin)

    async def run_one(index: int) -> RetrievalProbeSample:
        scheduled_at = origin + index / rate_per_second
        await asyncio.sleep(max(0.0, scheduled_at - time.monotonic()))
        started_at = time.monotonic()
        if index == 0 and started_event is not None:
            started_event.set()
        try:
            result_digest, revision_digest, result_count = await asyncio.wait_for(
                execute_retrieval_probe(session_factory=session_factory, spec=spec),
                timeout=timeout_seconds,
            )
            error: str | None = None
        except TimeoutError:
            result_digest, revision_digest, result_count = None, None, 0
            error = "timeout"
        except Exception as exception:
            result_digest, revision_digest, result_count = None, None, 0
            error = type(exception).__name__
        ended_at = time.monotonic()
        return RetrievalProbeSample(
            scheduled_offset_ms=1000 * index / rate_per_second,
            arrival_lag_ms=1000 * max(0.0, started_at - scheduled_at),
            duration_ms=1000 * (ended_at - started_at),
            result_digest=result_digest,
            revision_digest=revision_digest,
            result_count=result_count,
            error=error,
            actual_start_offset_ms=1000 * (started_at - origin),
            actual_end_offset_ms=1000 * (ended_at - origin),
        )

    try:
        if stop_event is None:
            request_count = int(duration_seconds * rate_per_second)
            return await asyncio.gather(
                *(run_one(index) for index in range(request_count))
            )
        tasks: list[asyncio.Task[RetrievalProbeSample]] = []
        index = 0
        while True:
            scheduled_at = origin + index / rate_per_second
            await asyncio.sleep(max(0.0, scheduled_at - time.monotonic()))
            tasks.append(asyncio.create_task(run_one(index)))
            if stop_event.is_set() and index / rate_per_second >= duration_seconds:
                break
            index += 1
        return await asyncio.gather(*tasks)
    finally:
        await engine.dispose()
