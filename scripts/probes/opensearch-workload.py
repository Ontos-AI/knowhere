"""Frozen open-loop mixed workload and strict, service-independent qualification.

The generated schedule preserves observed time offsets; ten copies do not turn
the profile into constant peak traffic. All offered arrivals survive producer
overload/cancellation in the denominators. No parser, model or cloud call is made
by validation, schedule generation or report evaluation.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
import gzip
from hashlib import sha256
import json
from math import ceil, isfinite
from pathlib import Path
import time
from typing import Literal, Self, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__: list[str] = ["WorkloadSupport"]
_CohortName = Literal["warm", "cold", "capacity_wait"]
_RESOURCE_MEASUREMENT_CONTRACT: dict[str, str] = {
    **dict.fromkeys((
        "pg_pool_wait_ms", "pg_transaction_ms", "native_http_pool_wait_ms", "native_response_bytes",
        "native_response_processing_ms", "inline_indexing_ms", "manual_retry_ms", "bulk_ms",
        "visibility_ms", "native_rejections",
    ), "cumulative-counter"),
    "pg_lock_wait_sampled_lower_bound_ms": "sampled-lower-bound",
    "gc_collection_time_ms": "collection-time-proxy",
    **dict.fromkeys((
        "native_connections", "native_task_queue", "cpu_percent", "heap_used_bytes", "disk_free_bytes",
        "open_indexes", "reserved_indexes", "uncertain_indexes", "catalog_indexes", "parse_task_backlog",
        "publication_age_ms", "pg_copy_age_ms",
    ), "gauge"),
}
_MISSING_EXACT_MEASUREMENTS: tuple[str, ...] = ("gc_stop_the_world_pause", "pg_lock_wait")


class _Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_utc: datetime
    end_utc: datetime
    path: Path
    sha256: str = Field(pattern="^[a-f0-9]{64}$")
    field_definitions: dict[str, str] = Field(min_length=1)
    missing_fields: list[str]
    derivation: str | None

    @model_validator(mode="after")
    def validateInterval(self) -> Self:
        if self.start_utc.utcoffset() != timedelta(0) or self.end_utc.utcoffset() != timedelta(0) or self.end_utc <= self.start_utc:
            raise ValueError("Source intervals require exact increasing UTC timestamps")
        return self


class _Segment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    phase: Literal["measured", "catchup"]
    source_offset_ms: int = Field(ge=0)
    duration_ms: int = Field(gt=0)
    target_offset_ms: int = Field(ge=0)


class _ReadCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    cohort: _CohortName
    namespace_size: Literal["hot", "large", "small"]
    cache_state: Literal["miss", "hit", "small_corpus"]
    request_path: Literal["/v1/retrieval/query", "/v2/retrieval/query"]
    payload_path: Path
    payload_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    quality_path: Path | None = None
    quality_sha256: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")
    credential_ref: str | None = None


class _PreparedCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    path: Path
    sha256: str = Field(pattern="^[a-f0-9]{64}$")
    source_revision: str = Field(min_length=1)
    source_file_name: str = Field(min_length=1)
    unit_count: int = Field(ge=0)
    compressed_bytes: int = Field(gt=0)
    uncompressed_bytes: int = Field(gt=0)
    native_bulk_bytes: list[int] = Field(min_length=1)
    representative_largest: bool


class _Cohort(BaseModel):
    model_config = ConfigDict(extra="forbid")
    planned_offered: int = Field(ge=1)
    minimum_completed_lexical: int = Field(ge=1)
    preconditioning: str = Field(min_length=1)
    expected_slot_state: Literal["open", "closed_capacity_available", "closed_capacity_wait"]
    receipt_sha256: str = Field(pattern="^[a-f0-9]{64}$")


class _Counts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reads: int = Field(gt=0)
    writes: int = Field(gt=0)
    measured_reads: int = Field(gt=0)
    catchup_reads: int = Field(gt=0)
    measured_writes: int = Field(gt=0)
    catchup_writes: int = Field(gt=0)
    rolling_peak_reads_per_second: Literal[50]


class _Burst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0, le=3_600_000)


class _Overlap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    kind: Literal["snapshot", "maintenance"]
    offset_ms: int = Field(ge=0, lt=3_600_000)


class _Runtime(BaseModel):
    model_config = ConfigDict(extra="forbid")
    maximum_inflight: int = Field(ge=1)
    queue_capacity: int = Field(ge=1)
    request_timeout_seconds: float = Field(gt=0)
    drain_grace_seconds: float = Field(gt=0)
    resource_sample_interval_seconds: float = Field(gt=0)
    maximum_offer_lag_ms: float = Field(ge=0)
    maximum_start_lag_ms: float = Field(ge=0)
    source_hash: str = Field(pattern="^[a-f0-9]{64}$")
    configuration_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    durability_receipt_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    staging_receipt_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    worker_queue: str = Field(pattern=r"^knowhere-prepared-[a-z0-9][a-z0-9_.-]{0,63}$")
    worker_concurrency: int = Field(ge=1)
    worker_prefetch: int = Field(ge=1)
    resource_measurement_contract: dict[str, str]

    @model_validator(mode="after")
    def validateMeasurements(self) -> Self:
        if self.resource_measurement_contract != _RESOURCE_MEASUREMENT_CONTRACT:
            raise ValueError("Resource fields must state exact gauges, counters and known measurement limits")
        return self


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["opensearch-mixed-workload-v1"]
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    provenance: Literal["observed", "reviewed_derived", "automated_test"]
    reviewer: str | None
    review_receipt_sha256: str | None = Field(pattern="^[a-f0-9]{64}$")
    seed: int
    measured_seconds: Literal[3600]
    catchup_seconds: Literal[300]
    multiplicity: Literal[10]
    multiplicity_offsets_ms: list[int] = Field(min_length=10, max_length=10)
    source_intervals: dict[Literal["reads", "writes"], _Source]
    segments: list[_Segment] = Field(min_length=2)
    read_cases: list[_ReadCase] = Field(min_length=1)
    prepared_cases: list[_PreparedCase] = Field(min_length=1)
    cohorts: dict[_CohortName, _Cohort]
    expected_counts: _Counts
    bursts: list[_Burst] = Field(min_length=1)
    overlap_events: list[_Overlap] = Field(min_length=2)
    runtime: _Runtime

    @model_validator(mode="after")
    def validateFrozenProfile(self) -> Self:
        if set(self.source_intervals) != {"reads", "writes"} or set(self.cohorts) != {"warm", "cold", "capacity_wait"}:
            raise ValueError("Freeze both source inventories and every latency cohort")
        if any(offset < 0 for offset in self.multiplicity_offsets_ms):
            raise ValueError("Multiplicity offsets cannot move requests before their source arrival")
        ordered: list[_Segment] = sorted(self.segments, key=lambda segment: segment.target_offset_ms)
        cursor: int = 0
        for segment in ordered:
            if segment.target_offset_ms != cursor:
                raise ValueError("Time mapping must cover the complete measured hour and catch-up without gaps")
            end: int = cursor + segment.duration_ms
            if segment.phase != ("measured" if cursor < 3_600_000 else "catchup") or cursor < 3_600_000 < end:
                raise ValueError("A time segment cannot cross measured/catch-up boundaries")
            for source in self.source_intervals.values():
                if segment.source_offset_ms + segment.duration_ms > (source.end_utc - source.start_utc).total_seconds() * 1000:
                    raise ValueError("Time mapping exceeds a frozen observed source interval")
            cursor = end
        if cursor != 3_900_000 or len({segment.id for segment in ordered}) != len(ordered):
            raise ValueError("Freeze unique segments covering exactly 3,900 seconds")
        if any(burst.end_ms <= burst.start_ms for burst in self.bursts):
            raise ValueError("Burst boundaries must be increasing and declared before execution")
        if {event.kind for event in self.overlap_events} != {"snapshot", "maintenance"}:
            raise ValueError("Freeze overlapping snapshot and maintenance schedules")
        if len({case.id for case in self.read_cases}) != len(self.read_cases) or len({case.id for case in self.prepared_cases}) != len(self.prepared_cases):
            raise ValueError("Case identities must be unique")
        for cohort in self.cohorts.values():
            if cohort.minimum_completed_lexical > cohort.planned_offered:
                raise ValueError("Cohort minima cannot exceed frozen planned counts")
        if any(size <= 0 for case in self.prepared_cases for size in case.native_bulk_bytes):
            raise ValueError("Native bulk-byte distributions must contain positive byte counts")
        return self


class _ReadArrival(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    offset_ms: int = Field(ge=0)
    case_id: str


class _WriteArrival(_ReadArrival):
    kind: Literal["create", "update", "retry", "supersede"]
    job_ids_by_segment: dict[str, list[str]]


def _isObservedCohort(name: str, row: dict[str, object]) -> bool:
    waited: float = float(str(row.get("capacity_wait_ms") or 0))
    if name == "capacity_wait":
        return waited > 0
    return waited == 0 and row.get("closed_recovery") is (name == "cold")


def _isUnclassifiedAdmission(row: dict[str, object], boundary: int) -> bool:
    upper: object = row.get("admitted_offset_ms")
    lower: object = row.get("admission_started_offset_ms")
    return upper is None or (lower is not None and float(str(lower)) <= boundary < float(str(upper)))


class WorkloadSupport:
    @staticmethod
    def parseManifest(value: object) -> _Manifest:
        return _Manifest.model_validate(value)

    @staticmethod
    def loadReadPayload(path: Path, expectedSha256: str) -> dict[str, object]:
        data: bytes = path.read_bytes()
        if sha256(data).hexdigest() != expectedSha256:
            raise ValueError("Read payload checksum differs from the frozen manifest")
        payload: object = json.loads(data)
        if not isinstance(payload, dict) or payload.get("use_agentic") is not False:
            raise ValueError("Mixed component load must explicitly disable agent/model calls")
        return cast(dict[str, object], payload)

    @staticmethod
    def loadPrepared(path: Path, *, expectedSha256: str, compressedBytes: int, uncompressedBytes: int) -> dict[str, object]:
        data: bytes = path.read_bytes()
        if len(data) != compressedBytes or sha256(data).hexdigest() != expectedSha256:
            raise ValueError("Prepared post-parse input checksum/bytes differ from the frozen manifest")
        with gzip.open(path, "rb") as stream:
            decoded: bytes = stream.read(uncompressedBytes + 1)
        if len(decoded) != uncompressedBytes:
            raise ValueError("Prepared post-parse uncompressed bytes differ from the frozen manifest")
        payload: object = json.loads(decoded)
        if not isinstance(payload, dict) or not isinstance(payload.get("chunks"), list):
            raise ValueError("Prepared completion requires retained parser chunks")
        return cast(dict[str, object], payload)

    @staticmethod
    def completePrepared(*, jobId: str, path: Path, expectedSha256: str,
            compressedBytes: int, uncompressedBytes: int,
            claim: Callable[[str, str], bool], finalize: Callable[[dict[str, object]], dict[str, object]],
            observe: Callable[[], dict[str, object]]) -> dict[str, object]:
        payload: dict[str, object] = WorkloadSupport.loadPrepared(path, expectedSha256=expectedSha256,
            compressedBytes=compressedBytes, uncompressedBytes=uncompressedBytes)
        if (not isinstance(payload.get("result_s3_key"), str) or not isinstance(payload.get("checksum"), str)
                or not isinstance(payload.get("zip_size"), int)):
            raise ValueError("Prepared completion requires retained result artifact identity and size")
        token: str = str(uuid4())
        if not claim(jobId, token):
            return {"status": "duplicate_no_execution", "job_id": jobId, "prepared_parser_boundary_only": True, **observe()}
        started: float = time.monotonic()
        response: dict[str, object] = finalize({"job_id": jobId,
            "result_s3_key": payload["result_s3_key"], "checksum": payload["checksum"], "zip_size": payload["zip_size"],
            "chunks": payload["chunks"], "stored_count": payload.get("stored_count", 0), "delivery_mode": "url",
            "section_summaries": payload.get("section_summaries"), "document_top_summary": payload.get("document_top_summary"),
            "parsingOwnerToken": token})
        actual: dict[str, object] = observe()
        return {**actual, "status": "success" if response.get("status") == "success" and actual.get("job_state") == "done" else "failed",
            "job_id": jobId, "prepared_parser_boundary_only": True,
            "verified_prepared_sha256": expectedSha256, "verified_compressed_bytes": compressedBytes,
            "verified_uncompressed_bytes": uncompressedBytes,
            "lifecycle_duration_ms": (time.monotonic() - started) * 1000}

    @staticmethod
    def buildSchedule(value: object, readArrivals: object, writeArrivals: object) -> dict[str, object]:
        manifest: _Manifest = _Manifest.model_validate(value)
        reads: list[_ReadArrival] = [_ReadArrival.model_validate(row) for row in cast(list[object], readArrivals)]
        writes: list[_WriteArrival] = [_WriteArrival.model_validate(row) for row in cast(list[object], writeArrivals)]
        if len({row.id for row in reads}) != len(reads) or len({row.id for row in writes}) != len(writes):
            raise ValueError("Source arrival IDs must be unique")
        cases: dict[str, _ReadCase] = {case.id: case for case in manifest.read_cases}
        prepared: set[str] = {case.id for case in manifest.prepared_cases}
        events: list[dict[str, object]] = []
        jobs: set[str] = set()
        for segment in manifest.segments:
            for kind, rows in (("read", reads), ("write", writes)):
                for row in rows:
                    if not segment.source_offset_ms <= row.offset_ms < segment.source_offset_ms + segment.duration_ms:
                        continue
                    if kind == "read" and row.case_id not in cases or kind == "write" and row.case_id not in prepared:
                        raise ValueError("Arrival refers to an unknown frozen case")
                    replicas: list[str] = []
                    if isinstance(row, _WriteArrival):
                        replicas = row.job_ids_by_segment.get(segment.id, [])
                        if len(replicas) != 10 or len(set(replicas)) != 10:
                            raise ValueError("Freeze ten exact predeclared write job replica identities for each mapped segment")
                    for copy, offset in enumerate(manifest.multiplicity_offsets_ms):
                        target: int = segment.target_offset_ms + row.offset_ms - segment.source_offset_ms + offset
                        if target >= segment.target_offset_ms + segment.duration_ms:
                            raise ValueError("Multiplicity offsets leave the frozen segment; do not drop arrivals")
                        event: dict[str, object] = {"id": f"{segment.id}:{kind}:{row.id}:{copy}", "kind": kind,
                            "case_id": row.case_id, "offset_ms": target, "phase": segment.phase,
                            "source_arrival_id": row.id, "replica": copy}
                        if kind == "read":
                            event["cohort"] = cases[row.case_id].cohort
                        else:
                            assert isinstance(row, _WriteArrival)
                            event.update({"job_id": replicas[copy], "write_kind": row.kind})
                            if replicas[copy] in jobs and row.kind != "retry":
                                raise ValueError("Distinct write arrivals cannot silently reuse a predeclared job")
                            jobs.add(replicas[copy])
                        events.append(event)
        readOffsets: list[int] = sorted(int(str(event["offset_ms"])) for event in events if event["kind"] == "read")
        window: deque[int] = deque()
        peak: int = 0
        for offset in readOffsets:
            while window and window[0] <= offset - 1000:
                window.popleft()
            window.append(offset)
            peak = max(peak, len(window))
        counts: dict[str, int] = {"reads": len(readOffsets), "writes": sum(event["kind"] == "write" for event in events),
            **{f"{phase}_{kind}s": sum(event["kind"] == kind and event["phase"] == phase for event in events)
                for phase in ("measured", "catchup") for kind in ("read", "write")},
            "rolling_peak_reads_per_second": peak}
        if counts != manifest.expected_counts.model_dump():
            raise ValueError("Generated counts/rolling peak differ from the frozen manifest")
        cohortCounts: Counter[str] = Counter(str(event["cohort"]) for event in events if event["kind"] == "read")
        if dict(cohortCounts) != {name: cohort.planned_offered for name, cohort in manifest.cohorts.items()}:
            raise ValueError("Generated cohort counts differ from the frozen manifest")
        events.extend({"id": "overlap:" + event.id, "kind": event.kind, "offset_ms": event.offset_ms, "phase": "measured"} for event in manifest.overlap_events)
        events.sort(key=lambda event: (int(str(event["offset_ms"])), str(event["id"])))
        return {"schema_version": "opensearch-generated-schedule-v1", "transformation_version": "repeat-translate-multiply-v1",
            "seed": manifest.seed, "counts": counts, "cohort_counts": dict(cohortCounts), "events": events,
            "schedule_sha256": sha256(json.dumps(events, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}

    @staticmethod
    async def runOpenLoop(events: list[dict[str, object]], *, execute: Callable[[dict[str, object]], Awaitable[dict[str, object]]],
            maximumInflight: int, queueCapacity: int, timeoutSeconds: float,
            waitUntil: Callable[[float], Awaitable[None]], now: Callable[[], float],
            record: Callable[[dict[str, object]], None], finishAtMs: float | None = None,
            drainGraceSeconds: float | None = None,
            eventTimeout: Callable[[dict[str, object]], float] | None = None) -> dict[str, object]:
        if maximumInflight < 1 or queueCapacity < 1 or timeoutSeconds <= 0:
            raise ValueError("Freeze positive runner resource bounds")
        queue: asyncio.Queue[tuple[dict[str, object], float] | None] = asyncio.Queue(maxsize=queueCapacity)
        observations: list[dict[str, object]] = []
        offered: list[dict[str, object]] = []

        async def consume() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    return
                event, offeredAt = item
                started: float = now()
                record({"event": "started", "id": event["id"], "started_offset_ms": started})
                observation: dict[str, object]
                try:
                    observation = await asyncio.wait_for(execute(event), timeout=eventTimeout(event) if eventTimeout else timeoutSeconds)
                    observation = {**observation, "id": event["id"], "offered_offset_ms": offeredAt, "started_offset_ms": started, "completed_offset_ms": now()}
                except asyncio.TimeoutError:
                    observation = {"id": event["id"], "status": "timeout", "offered_offset_ms": offeredAt, "started_offset_ms": started, "completed_offset_ms": now()}
                except asyncio.CancelledError:
                    observation = {"id": event["id"], "status": "cancelled", "offered_offset_ms": offeredAt, "started_offset_ms": started, "completed_offset_ms": now()}
                    observations.append(observation)
                    record({"event": "completed", **observation})
                    queue.task_done()
                    raise
                except Exception as err:
                    observation = {"id": event["id"], "status": "failed", "error_type": type(err).__name__, "offered_offset_ms": offeredAt, "started_offset_ms": started, "completed_offset_ms": now()}
                observations.append(observation)
                record({"event": "completed", **observation})
                queue.task_done()

        workers: list[asyncio.Task[None]] = [asyncio.create_task(consume()) for _ in range(maximumInflight)]
        wasCancelled: bool = False
        try:
            for event in events:
                await waitUntil(float(str(event["offset_ms"])))
                stamp: float = now()
                offer: dict[str, object] = {"event": "offered", "id": event["id"], "scheduled_offset_ms": event["offset_ms"], "offered_offset_ms": stamp}
                offered.append(offer)
                record(offer)
                try:
                    queue.put_nowait((event, stamp))
                except asyncio.QueueFull:
                    rejected: dict[str, object] = {"id": event["id"], "status": "producer_rejected", "offered_offset_ms": stamp}
                    observations.append(rejected)
                    record({"event": "completed", **rejected})
            if finishAtMs is not None:
                await waitUntil(finishAtMs)
            try:
                await asyncio.wait_for(queue.join(), timeout=drainGraceSeconds)
            except asyncio.TimeoutError:
                wasCancelled = True
        except asyncio.CancelledError:
            wasCancelled = True
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            while not queue.empty():
                pending = queue.get_nowait()
                if pending is not None:
                    event, offeredAt = pending
                    cancelled: dict[str, object] = {"id": event["id"], "status": "cancelled", "offered_offset_ms": offeredAt}
                    observations.append(cancelled)
                    record({"event": "completed", **cancelled})
                queue.task_done()
        return {"completed": not wasCancelled, "cancelled": wasCancelled, "offered": len(offered), "offer_records": offered,
            "producer_rejected": sum(row["status"] == "producer_rejected" for row in observations), "observations": observations}

    @staticmethod
    def evaluateReport(value: object, schedule: dict[str, object], report: dict[str, object]) -> dict[str, object]:
        manifest: _Manifest = _Manifest.model_validate(value)
        events: list[dict[str, object]] = cast(list[dict[str, object]], schedule["events"])
        expected: dict[str, dict[str, object]] = {str(event["id"]): event for event in events}
        failures: list[str] = []
        observations: list[dict[str, object]] = []
        countFields: tuple[str, ...] = ("lexical_executions", "path_channel_calls", "content_channel_calls", "sdk_retries")
        numberFields: tuple[str, ...] = ("lexical_ms", "cold_first_ms", "capacity_wait_ms", "wait_to_discovery_ms",
            "full_request_ms", "indexing_operation_ms", "admission_started_offset_ms", "admitted_offset_ms", "search_ready_offset_ms")
        for original in cast(list[dict[str, object]], report.get("observations", [])):
            row: dict[str, object] = dict(original)
            if expected.get(str(row.get("id")), {}).get("kind") == "read" and not isinstance(row.get("lexical_executions"), int):
                failures.append("missing-or-invalid-native-execution-observation")
            for field in countFields:
                count: object = row.get(field, 0)
                if count is None:
                    row[field] = 0
                elif not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    failures.append("invalid-count-observation")
                    row[field] = 0
            for field in numberFields:
                number: object = row.get(field)
                if number is not None and (not isinstance(number, (int, float)) or isinstance(number, bool) or not isfinite(number) or number < 0):
                    failures.append("invalid-timing-observation")
                    row[field] = None
            observations.append(row)
        observed: dict[str, dict[str, object]] = {str(row["id"]): row for row in observations}
        offers: list[dict[str, object]] = cast(list[dict[str, object]], report.get("offer_records", []))
        if report.get("telemetry_export_error"):
            failures.append("source-stage-telemetry-export-incomplete")
        if report.get("completed") is not True or report.get("cancelled"):
            failures.append("cancelled-or-incomplete-campaign")
        if report.get("actual_staging_campaign") is not True:
            failures.append("not-an-actual-staging-campaign")
        if float(str(report.get("measured_elapsed_ms", 0))) < 3_600_000 or float(str(report.get("catchup_elapsed_ms", 0))) < 300_000:
            failures.append("measured-hour-or-five-minute-catchup-incomplete")
        if len(observed) != len(observations) or set(observed) != set(expected):
            failures.append("missing-duplicate-or-unexpected-completions")
        if {str(row["id"]) for row in offers} != set(expected) or len(offers) != len(expected):
            failures.append("missing-duplicate-or-unexpected-offered-arrivals")
        offerLags: list[float] = []
        startLags: list[float] = []
        byOffer: dict[str, dict[str, object]] = {str(row["id"]): row for row in offers}
        for identity, event in expected.items():
            offeredAt: object = byOffer.get(identity, {}).get("offered_offset_ms")
            startedAt: object = observed.get(identity, {}).get("started_offset_ms")
            if not isinstance(offeredAt, (int, float)) or isinstance(offeredAt, bool) or not isfinite(offeredAt):
                failures.append("missing-producer-offer-timestamp")
                continue
            lag: float = offeredAt - float(str(event["offset_ms"]))
            offerLags.append(lag)
            if abs(lag) > manifest.runtime.maximum_offer_lag_ms:
                failures.append("producer-offer-profile-jitter-exceeded")
            if isinstance(startedAt, (int, float)) and not isinstance(startedAt, bool) and isfinite(startedAt):
                startLag: float = startedAt - offeredAt
                startLags.append(startLag)
                if startLag < 0 or startLag > manifest.runtime.maximum_start_lag_ms:
                    failures.append("producer-dispatch-profile-jitter-exceeded")
            elif observed.get(identity, {}).get("status") == "success":
                failures.append("missing-producer-start-timestamp")
        def isVerifiedExclusion(row: dict[str, object]) -> bool:
            return row.get("exclusion_verified") is True and row.get("exclusion_reason") in ("archived", "superseded")

        if any(row.get("status") != "success" and not isVerifiedExclusion(row) for row in observations):
            failures.append("normal-load-failed-timeout-rejected-or-cancelled-work")
        cohorts: dict[str, dict[str, object]] = {}
        for name, cohort in manifest.cohorts.items():
            precondition: dict[str, object] = cast(dict[str, object], cast(dict[str, object], report.get("precondition_receipts", {})).get(name, {}))
            if precondition.get("mode") != "actual" or precondition.get("artifact_sha256") != cohort.receipt_sha256 or precondition.get("observed_slot_state") != cohort.expected_slot_state:
                failures.append("missing-or-incompatible-preconditioning:" + name)
            members: list[dict[str, object]] = [observed[identity] for identity, event in expected.items()
                if event.get("cohort") == name and identity in observed]
            timingField: str = {"warm": "lexical_ms", "cold": "cold_first_ms", "capacity_wait": "wait_to_discovery_ms"}[name]
            samples: list[float] = sorted(float(str(row[timingField])) for row in members if row.get("status") == "success"
                and int(str(row.get("lexical_executions", 0))) > 0 and row.get(timingField) is not None
                and row.get("quality_passed") is True and row.get("identity_passed") is True
                and _isObservedCohort(name, row))
            percentile: float | None = samples[max(0, ceil(len(samples) * 0.95) - 1)] if samples else None
            target: int = {"warm": 1000, "cold": 3000, "capacity_wait": 8000}[name]
            passed: bool = len(samples) >= cohort.minimum_completed_lexical and percentile is not None and percentile <= target
            cohorts[name] = {"planned_offered": cohort.planned_offered, "minimum_completed_lexical": cohort.minimum_completed_lexical,
                "completed_lexical_samples": len(samples), "p95_ms": percentile, "target_p95_ms": target, "passed": passed}
            if not passed:
                failures.append("insufficient-or-failed-latency-cohort:" + name)
        for identity, event in expected.items():
            row: dict[str, object] = observed.get(identity, {})
            if event["kind"] == "read" and (row.get("quality_passed") is not True or row.get("identity_passed") is not True or row.get("full_request_ms") is None):
                failures.append("missing-or-failed-final-public-quality-identity-timing")
            if event["kind"] == "read" and float(str(row.get("capacity_wait_ms") or 0)) > 5000:
                failures.append("capacity-wait-exceeded-five-second-budget")
            if event["kind"] == "write" and not isVerifiedExclusion(row) and (row.get("search_ready_offset_ms") is None or row.get("job_state") != "done"
                    or row.get("indexing_operation_ms") is None or float(str(row.get("indexing_operation_ms", 0))) > 10000):
                failures.append("normal-load-indexing-not-ready-or-deadline-failed")
            if event["kind"] == "write" and not isVerifiedExclusion(row):
                if row.get("admitted_offset_ms") is None:
                    failures.append("missing-write-admission-timestamp")
                prepared: _PreparedCase = next(case for case in manifest.prepared_cases if case.id == event["case_id"])
                if row.get("native_bulk_bytes") != prepared.native_bulk_bytes:
                    failures.append("missing-or-changed-native-bulk-byte-distribution")
        catchups: list[dict[str, object]] = []
        for burst in manifest.bursts:
            unclassified: list[str] = [identity for identity, event in expected.items() if event["kind"] == "write"
                and float(str(event["offset_ms"])) <= burst.end_ms and not isVerifiedExclusion(observed.get(identity, {}))
                and _isUnclassifiedAdmission(observed.get(identity, {}), burst.end_ms)]
            members: list[dict[str, object]] = [row for identity, row in observed.items() if expected.get(identity, {}).get("kind") == "write"
                and row.get("admitted_offset_ms") is not None and float(str(row["admitted_offset_ms"])) <= burst.end_ms
                and (row.get("search_ready_offset_ms") is None or float(str(row["search_ready_offset_ms"])) > burst.end_ms)]
            exclusions: list[dict[str, object]] = [row for identity, row in observed.items()
                if expected.get(identity, {}).get("kind") == "write" and float(str(expected[identity]["offset_ms"])) <= burst.end_ms
                and isVerifiedExclusion(row)]
            excludedIds: set[str] = {str(row["id"]) for row in exclusions}
            eligible: list[dict[str, object]] = [row for row in members if str(row["id"]) not in excludedIds]
            caught: bool = not unclassified and all(row.get("job_state") == "done" and row.get("search_ready_offset_ms") is not None
                and float(str(row["search_ready_offset_ms"])) <= burst.end_ms + 300_000 for row in eligible)
            catchups.append({"burst": burst.id, "boundary_ms": burst.end_ms, "deadline_ms": burst.end_ms + 300_000,
                "frozen_eligible_ids": [row["id"] for row in eligible], "exclusions": [{"id": row["id"],
                    "job_id": expected[str(row["id"])]["job_id"], "reason": row["exclusion_reason"]} for row in exclusions], "passed": caught})
            catchups[-1]["unclassified_offered_write_ids"] = unclassified
            if not caught:
                failures.append("burst-catchup-failed:" + burst.id)
        resources: list[dict[str, object]] = cast(list[dict[str, object]], report.get("resource_samples", []))
        if not resources or any(row.get("complete") is not True or row.get("source_hash") != manifest.runtime.source_hash
                or not isinstance(row.get("metrics"), dict)
                or row.get("measurement_kinds") != manifest.runtime.resource_measurement_contract
                or not isinstance(row.get("missing_exact_measurements"), list)
                or sorted(map(str, cast(list[object], row["missing_exact_measurements"]))) != list(_MISSING_EXACT_MEASUREMENTS)
                or not _RESOURCE_MEASUREMENT_CONTRACT.keys() <= cast(dict[str, object], row["metrics"]).keys()
                or any(not isinstance(cast(dict[str, object], row["metrics"])[field], (int, float))
                    or isinstance(cast(dict[str, object], row["metrics"])[field], bool)
                    or not isfinite(cast(float, cast(dict[str, object], row["metrics"])[field])) for field in _RESOURCE_MEASUREMENT_CONTRACT)
                for row in resources):
            failures.append("missing-or-incomplete-stage-and-resource-measurements")
        minimumSamples: int = int(3_900 / manifest.runtime.resource_sample_interval_seconds) + 1
        if len(resources) < minimumSamples:
            failures.append("resource-sampling-window-incomplete")
        overlap: list[dict[str, object]] = cast(list[dict[str, object]], report.get("overlap_results", []))
        if not any(row.get("kind") == "snapshot" and row.get("coverage_verified") is True and float(str(row.get("started_offset_ms", 3_600_001))) < 3_600_000
                and row.get("completed_offset_ms") is not None and row.get("overlapped_indexing_or_lru") is True for row in overlap):
            failures.append("missing-complete-covered-overlapping-full-snapshot")
        if not any(row.get("kind") == "maintenance" and row.get("completed") is True for row in overlap):
            failures.append("missing-overlapping-maintenance")
        if manifest.provenance == "automated_test" or not manifest.reviewer or not manifest.review_receipt_sha256:
            failures.append("workload-source-and-derivation-not-human-reviewed")
        if not any(case.representative_largest and case.unit_count >= 1862 for case in manifest.prepared_cases):
            failures.append("retained-1862-unit-document-not-represented")
        return {"schema_version": "opensearch-mixed-qualification-v1", "qualified": not failures,
            "failures": sorted(set(failures)), "cohorts": cohorts, "catchup": catchups,
            "denominators": {"expected_reads": manifest.expected_counts.reads, "expected_writes": manifest.expected_counts.writes,
                "offered_total": len(offers), "started_total": sum(row.get("started_offset_ms") is not None for row in observations),
                "completed_reads": sum(expected.get(str(row["id"]), {}).get("kind") == "read" and row.get("status") == "success" for row in observations),
                "failed_or_cancelled": sum(row.get("status") != "success" for row in observations),
                "verified_domain_exclusions": sum(isVerifiedExclusion(row) for row in observations),
                "lexical_executions": sum(int(str(row.get("lexical_executions", 0))) for row in observations),
                "path_channel_calls": sum(int(str(row.get("path_channel_calls", 0))) for row in observations),
                "content_channel_calls": sum(int(str(row.get("content_channel_calls", 0))) for row in observations),
                "cache_or_small_corpus_bypasses": sum(row.get("bypass") in ("cache", "small_corpus") for row in observations),
                "sdk_retries": sum(int(str(row.get("sdk_retries", 0))) for row in observations)},
            "producer_profile": {"maximum_offer_lag_ms": max(offerLags) if offerLags else None,
                "maximum_start_lag_ms": max(startLags) if startLags else None,
                "frozen_maximum_offer_lag_ms": manifest.runtime.maximum_offer_lag_ms,
                "frozen_maximum_start_lag_ms": manifest.runtime.maximum_start_lag_ms},
            "resource_measurement_contract": manifest.runtime.resource_measurement_contract,
            "missing_exact_resource_measurements": list(_MISSING_EXACT_MEASUREMENTS), "resource_sample_count": len(resources), "actual_staging_campaign": report.get("actual_staging_campaign") is True}
