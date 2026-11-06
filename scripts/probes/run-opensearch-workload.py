"""Validate or execute a frozen private one-hour/10x mixed staging workload.

Default: offline dry-run with checksums/counts and missing acceptance inputs.
--simulate: accelerated automated component exercise; never staging qualification.
--execute: real HTTP and explicitly registered prepared-completion Celery tasks,
requiring reviewed input/configuration/preconditioning and bounded campaign approval.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import ssl
import signal
import sys
import time
from types import ModuleType
from typing import Protocol, cast
from urllib.parse import unquote, urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx


class _Source(Protocol):
    path: Path
    sha256: str


class _ReadCase(Protocol):
    id: str
    namespace: str
    namespace_size: str
    request_path: str
    payload_path: Path
    payload_sha256: str
    quality_path: Path | None
    quality_sha256: str | None
    credential_ref: str | None


class _PreparedCase(Protocol):
    id: str
    path: Path
    sha256: str
    compressed_bytes: int
    uncompressed_bytes: int
    unit_count: int
    representative_largest: bool
    source_file_name: str


class _Runtime(Protocol):
    maximum_inflight: int
    queue_capacity: int
    request_timeout_seconds: float
    drain_grace_seconds: float
    resource_sample_interval_seconds: float
    source_hash: str
    configuration_sha256: str
    staging_receipt_sha256: str
    durability_receipt_sha256: str
    worker_queue: str


class _Campaign(Protocol):
    id: str
    provenance: str
    reviewer: str | None
    review_receipt_sha256: str | None
    source_intervals: dict[str, _Source]
    read_cases: list[_ReadCase]
    prepared_cases: list[_PreparedCase]
    runtime: _Runtime


def _loadSupport() -> ModuleType:
    path: Path = Path(__file__).with_name("opensearch-workload.py")
    specification = importlib.util.spec_from_file_location("mixed_workload_support", path)
    if specification is None or specification.loader is None:
        raise ValueError("Mixed-workload support was not installed")
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _readJson(path: Path) -> object:
    return json.loads(path.read_bytes())


def _validateBindings(configuration: dict[str, object], manifest: _Campaign,
        schedule: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    root: Path = Path(__file__).resolve().parents[2]
    sourceScript: Path = root / "scripts/probes/workload-source-adapter.py"
    snapshotScript: Path = root / "deploy/opensearch/workload_snapshot.py"
    bindings: dict[str, object] = _object(configuration["bindings"])
    sourcePaths: set[Path] = set()
    for event in ("precondition", "admission", "resource", "maintenance", "stage-export", "snapshot"):
        binding: dict[str, object] = _object(bindings[event])
        script: Path = sourceScript if event != "snapshot" else snapshotScript
        if Path(str(binding["script_path"])).resolve() != script or sha256(script.read_bytes()).hexdigest() != binding["script_sha256"]:
            raise ValueError("Mandatory staging bindings must use the delivered reviewed source adapters")
        argv: list[str] = cast(list[str], binding["argv"])
        if argv[:2] != [sys.executable, str(script)] or len(argv) != 7 or argv[2] != "--configuration" or argv[4] != "--expected-configuration-sha256" or argv[6] != "--execute":
            raise ValueError("Binding must execute the reviewed adapter and exact private configuration")
        configPath: Path = Path(argv[3]).resolve()
        if sha256(configPath.read_bytes()).hexdigest() != argv[5]:
            raise ValueError("Private source adapter configuration checksum changed")
        if event != "snapshot":
            sourcePaths.add(configPath)
    if len(sourcePaths) != 1:
        raise ValueError("All source adapters must bind one campaign configuration")
    source: dict[str, object] = _object(_readJson(sourcePaths.pop()))
    issuer: dict[str, object] = _object(configuration["issuer"])
    if source.get("issuer") != issuer or source.get("campaign_id") != manifest.id or source.get("source_hash") != manifest.runtime.source_hash:
        raise ValueError("Source adapter configuration does not bind this campaign and one physical issuer")
    cases: dict[str, object] = _object(source["cases"])
    prepared: dict[str, _PreparedCase] = {case.id: case for case in manifest.prepared_cases}
    owners: dict[str, object] = _object(configuration["job_owners"])
    workerPaths: dict[str, object] = _object(configuration["prepared_worker_paths"])
    for arrival in cast(list[dict[str, object]], schedule["events"]):
        if arrival["kind"] != "write":
            continue
        caseId: str = str(arrival["case_id"])
        jobId: str = str(arrival["job_id"])
        case: dict[str, object] = _object(cases[caseId])
        reference: _PreparedCase = prepared[caseId]
        if (jobId not in cast(list[str], case["job_ids"]) or case.get("owner_id") != owners.get(jobId)
                or case.get("prepared_path") != workerPaths.get(caseId) or case.get("sha256") != reference.sha256
                or case.get("source_file_name") != reference.source_file_name
                or case.get("compressed_bytes") != reference.compressed_bytes
                or case.get("uncompressed_bytes") != reference.uncompressed_bytes or case.get("unit_count") != reference.unit_count):
            raise ValueError("A source admission differs from the frozen prepared case or scheduled job")
    return source, issuer


def _executeApproved(arguments: argparse.Namespace, support: ModuleType, manifest: _Campaign,
        schedule: dict[str, object], payloads: dict[str, dict[str, object]], configuration: dict[str, object]) -> dict[str, object]:
    root: Path = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root), str(root / "deploy/opensearch"), str(root / "packages/shared-python")]
    from deploy.opensearch.authority import OperatorAuthority
    from deploy.opensearch.execution import OperationExecution
    from deploy.opensearch.operations import OpenSearchOperations

    _, issuerInput = _validateBindings(configuration, manifest, schedule)
    manifestPath: Path = Path(str(configuration["operator_manifest_path"]))
    installation: dict[str, object] = _object(_readJson(manifestPath))
    approval: dict[str, object] = _object(_readJson(arguments.approval))
    installationReceipt: dict[str, object] = _object(OpenSearchOperations.renderBundle(installation)["installation-receipt.json"])
    stateDirectory: Path = Path(str(configuration["operation_state_directory"]))
    OperationExecution.validateOperatorApproval(approval, stateDirectory)
    issuerPath: Path = Path(str(issuerInput["receipt_path"]))
    if (approval.get("installation_manifest_sha256") != installationReceipt["manifest_sha256"]
            or issuerInput.get("invocation_token") != arguments.issuer_token
            or issuerInput.get("host_identity") != OperationExecution.getHostFingerprint()
            or issuerPath.exists()):
        raise ValueError("Campaign launch must bind a fresh reviewed physical issuer and private receipt")
    arguments.expected_issuer_token = None
    arguments.fence_receipt = None
    with OperatorAuthority.fromArguments(arguments, installation, operationId=str(issuerInput["operation_id"]),
            sourceHash=manifest.runtime.source_hash, epoch=int(str(issuerInput["epoch"]))) as authority:
        with OperationExecution.claim(stateDirectory, resourceId=str(authority.receipt["logical_cluster_id"]),
                operationId=authority.operationId, authorityReceipt=authority.receipt) as lease:
            _writeJson(issuerPath, authority.receipt)
            authority.beforeMutation()
            report: dict[str, object] = asyncio.run(_run(arguments, support, manifest, schedule, payloads, configuration,
                recordGroup=lease.recordProcessGroup))
            observations: list[dict[str, object]] = cast(list[dict[str, object]], report["observations"])
            if (report["completed"] is True and not report.get("telemetry_export_error")
                    and all(row.get("status") in ("success", "excluded", "producer_rejected") for row in observations)):
                lease.complete()
                authority.complete()
            return report


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected a frozen JSON object")
    return cast(dict[str, object], value)


def _writeJson(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def _readSecret(path: Path) -> str:
    if not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 65536:
        raise ValueError("Campaign secret files require bounded regular files and private permissions")
    value: str = path.read_text().rstrip("\r\n")
    if not value:
        raise ValueError("Campaign secret file is empty")
    return value


def _validateFiles(path: Path, support: ModuleType) -> tuple[_Campaign, dict[str, object], dict[str, dict[str, object]], list[str]]:
    manifest: _Campaign = cast(_Campaign, support.WorkloadSupport.parseManifest(_readJson(path)))
    sources: dict[str, object] = {}
    for kind, source in manifest.source_intervals.items():
        sourcePath: Path = path.parent / source.path
        if sha256(sourcePath.read_bytes()).hexdigest() != source.sha256:
            raise ValueError("A source arrival checksum changed")
        sources[kind] = _readJson(sourcePath)
    payloads: dict[str, dict[str, object]] = {}
    missing: list[str] = []
    for case in manifest.read_cases:
        payloads[case.id] = support.WorkloadSupport.loadReadPayload(path.parent / case.payload_path, case.payload_sha256)
        if payloads[case.id].get("namespace") != case.namespace:
            raise ValueError("Read namespace differs from its frozen schedule")
        if case.quality_path is None or case.quality_sha256 is None:
            missing.append("reviewed-public-quality-labels:" + case.id)
        elif sha256((path.parent / case.quality_path).read_bytes()).hexdigest() != case.quality_sha256:
            raise ValueError("A frozen final-public quality input changed")
    for case in manifest.prepared_cases:
        support.WorkloadSupport.loadPrepared(path.parent / case.path, expectedSha256=case.sha256,
            compressedBytes=case.compressed_bytes, uncompressedBytes=case.uncompressed_bytes)
    if manifest.provenance == "automated_test" or not manifest.reviewer or not manifest.review_receipt_sha256:
        missing.append("automated-inputs-are-not-reviewed-production-arrivals")
    if not any(case.representative_largest and case.unit_count >= 1862 for case in manifest.prepared_cases):
        missing.append("actual-retained-1862-unit-prepared-document")
    if {case.namespace_size for case in manifest.read_cases} != {"hot", "large", "small"}:
        missing.append("reviewed-hot-largest-and-small-namespace-distribution")
    schedule: dict[str, object] = support.WorkloadSupport.buildSchedule(_readJson(path), sources["reads"], sources["writes"])
    writeKinds: set[object] = {event.get("write_kind") for event in cast(list[dict[str, object]], schedule["events"]) if event["kind"] == "write"}
    if not {"update", "retry", "supersede"} <= writeKinds:
        missing.append("reviewed-update-retry-supersession-arrivals-and-admission")
    return manifest, schedule, payloads, missing


def _verifyQuality(response: dict[str, object], quality: dict[str, object]) -> tuple[bool, bool]:
    rows: list[dict[str, object]] = cast(list[dict[str, object]], response.get("results", []))
    canonical: dict[tuple[str, str], dict[str, object]] = {
        (str(row["document_id"]), str(row["chunk_id"])): row
        for row in cast(list[dict[str, object]], quality.get("canonical_rows", []))
    }
    observed: set[tuple[str, str]] = set()
    identity: bool = (bool(canonical) and response.get("namespace") == quality.get("namespace")
        and isinstance(quality.get("top_k"), int) and len(rows) <= int(str(quality["top_k"])))
    for row in rows:
        source: dict[str, object] = _object(row.get("source"))
        key: tuple[str, str] = (str(source.get("document_id")), str(row.get("chunk_id")))
        expected: dict[str, object] | None = canonical.get(key)
        if expected is None:
            identity = False
            continue
        observed.add(key)
        identity = identity and (
            row.get("job_result_id") == source.get("job_result_id") == expected.get("job_result_id")
            and source.get("section_path") == expected.get("section_path")
            and source.get("source_file_name") == expected.get("source_file_name")
            and row.get("chunk_type") == expected.get("chunk_type")
            and sha256(str(row.get("content") or "").encode()).hexdigest() == expected.get("content_sha256")
            and row.get("file_path") == expected.get("file_path")
            and source.get("page_nums", []) == expected.get("page_nums", [])
        )
        if expected.get("asset_path") is not None:
            identity = identity and unquote(urlsplit(str(row.get("asset_url") or "")).path) == expected["asset_path"]
    groups: list[dict[str, object]] = cast(list[dict[str, object]], quality.get("evidence_groups", []))
    passed: bool = bool(groups) and all(group.get("postgresql_found") is False or any(
        (str(evidence["document_id"]), str(evidence["chunk_id"])) in observed
        for evidence in cast(list[dict[str, object]], group["alternatives"])) for group in groups)
    return passed, identity


def _joinTelemetry(report: dict[str, object], telemetry: list[dict[str, object]], *, campaignId: str, sourceHash: str) -> None:
    selected: list[dict[str, object]] = [row for row in telemetry if row.get("mode") == "actual"
        and row.get("source_hash") == sourceHash and row.get("campaign_id") == campaignId]
    byId: dict[str, dict[str, object]] = {str(row["id"]): row for row in selected}
    if len(byId) != len(selected):
        raise ValueError("Duplicate telemetry for an exact campaign event identity")
    for observation in cast(list[dict[str, object]], report["observations"]):
        extra: dict[str, object] = byId.get(str(observation["id"]), {})
        for key in ("lexical_ms", "cold_first_ms", "capacity_wait_ms", "wait_to_discovery_ms", "lexical_executions",
                "path_channel_calls", "content_channel_calls", "bypass", "indexing_operation_ms", "native_bulk_bytes",
                "exclusion_reason", "exclusion_verified", "indexing_intervals_utc", "lru_intervals_utc"):
            if key in extra:
                observation[key] = extra[key]
        if extra.get("lexical_executions"):
            observation["closed_recovery"] = extra.get("cold_first_ms") is not None


def _readInterval(value: dict[str, object]) -> tuple[datetime, datetime]:
    start: datetime = datetime.fromisoformat(str(value["started_at_utc"]))
    end: datetime = datetime.fromisoformat(str(value["finished_at_utc"]))
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("Overlap requires measured increasing UTC intervals")
    return start, end


def _joinSnapshotOverlap(report: dict[str, object]) -> None:
    intervals: list[tuple[datetime, datetime, str, str]] = []
    for row in cast(list[dict[str, object]], report["observations"]):
        for kind in ("indexing", "lru"):
            for interval in cast(list[dict[str, object]], row.get(kind + "_intervals_utc", [])):
                start, end = _readInterval(interval)
                intervals.append((start, end, str(row["id"]), kind))
    for row in cast(list[dict[str, object]], report["overlap_results"]):
        if row.get("kind") != "snapshot":
            continue
        row["overlapped_indexing_or_lru"] = False
        if not isinstance(row.get("snapshot_interval_utc"), dict):
            continue
        start, end = _readInterval(_object(row["snapshot_interval_utc"]))
        matches: list[dict[str, str]] = [{"event_id": identity, "kind": kind}
            for otherStart, otherEnd, identity, kind in intervals if otherStart < end and start < otherEnd]
        row["overlap_witnesses"] = matches
        row["overlapped_indexing_or_lru"] = bool(matches)


async def _runBinding(binding: dict[str, object], request: dict[str, object], *, directory: Path,
        sourceHash: str, campaignId: str, recordGroup: Callable[[int], None] | None = None) -> dict[str, object]:
    script: Path = Path(str(binding["script_path"]))
    if sha256(script.read_bytes()).hexdigest() != binding["script_sha256"]:
        raise ValueError("An operational binding changed from the reviewed executable")
    request = {**request, "campaign_id": campaignId, "source_hash": sourceHash}
    identity: str = sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:24]
    requestPath: Path = directory / (identity + "-request.json")
    receiptPath: Path = directory / (identity + "-receipt.json")
    _writeJson(requestPath, request)
    command: list[str] = cast(list[str], binding["argv"])
    if str(script) not in command:
        raise ValueError("Binding argv must invoke the exact reviewed source script")
    process = await asyncio.create_subprocess_exec(*command, "--request", str(requestPath), "--receipt", str(receiptPath),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        if recordGroup is not None:
            recordGroup(process.pid)
        await asyncio.wait_for(process.wait(), timeout=float(str(binding["timeout_seconds"])))
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError("Operational binding left a descendant issuer")
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.wait_for(process.wait(), timeout=2)
        raise
    receipt: dict[str, object] = _object(_readJson(receiptPath))
    receiptPath.chmod(0o600)
    if process.returncode != 0 or receipt.get("mode") != "actual" or receipt.get("status") != "passed" or receipt.get("errors") != [] or receipt.get("source_hash") != sourceHash or receipt.get("campaign_id") != campaignId:
        raise ValueError("Operational binding lacks an actual source/campaign-bound receipt")
    return receipt


async def _run(arguments: argparse.Namespace, support: ModuleType, manifest: _Campaign,
        schedule: dict[str, object], payloads: dict[str, dict[str, object]], configuration: dict[str, object],
        recordGroup: Callable[[int], None] | None = None, *, runWindowMs: int = 3_900_000) -> dict[str, object]:
    directory: Path = arguments.output.resolve()
    directory.mkdir(mode=0o700)
    (directory / "binding-receipts").mkdir(mode=0o700)
    _writeJson(directory / "frozen-manifest.json", _readJson(arguments.manifest))
    _writeJson(directory / "generated-schedule.json", schedule)
    events: list[dict[str, object]] = cast(list[dict[str, object]], schedule["events"])
    simulated: bool = arguments.simulate
    current: float = 0.0
    origin: float = time.monotonic()
    startedUtc: str = datetime.now(timezone.utc).isoformat()
    bindings: dict[str, dict[str, object]] = cast(dict[str, dict[str, object]], configuration.get("bindings", {}))

    async def bind(name: str, request: dict[str, object]) -> dict[str, object]:
        return await _runBinding(bindings[name], request, directory=directory / "binding-receipts",
            sourceHash=manifest.runtime.source_hash, campaignId=manifest.id, recordGroup=recordGroup)

    preconditions: dict[str, object] = {}
    if not simulated:
        receipt: dict[str, object] = await bind("precondition", {"event": "precondition", "manifest_path": str(arguments.manifest.resolve())})
        preconditions = _object(receipt["cohorts"])
        origin = time.monotonic()
        startedUtc = datetime.now(timezone.utc).isoformat()

    def now() -> float:
        return current if simulated else (time.monotonic() - origin) * 1000

    async def waitUntil(offset: float) -> None:
        nonlocal current
        if simulated:
            current = offset
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(max(0, (offset - now()) / 1000))

    resources: list[dict[str, object]] = []
    overlaps: list[dict[str, object]] = []
    cases: dict[str, _ReadCase] = {case.id: case for case in manifest.read_cases}
    prepared: dict[str, _PreparedCase] = {case.id: case for case in manifest.prepared_cases}
    eventStream = (directory / "events.jsonl").open("x", encoding="utf-8")
    (directory / "events.jsonl").chmod(0o600)

    def record(value: dict[str, object]) -> None:
        eventStream.write(json.dumps(value, separators=(",", ":")) + "\n")
        eventStream.flush()

    async def sampleResources() -> None:
        if simulated:
            return
        sample: int = 0
        while sample * manifest.runtime.resource_sample_interval_seconds <= runWindowMs / 1000:
            offset: float = sample * manifest.runtime.resource_sample_interval_seconds * 1000
            await waitUntil(offset)
            try:
                receipt = await bind("resource", {"event": "resource", "sample": sample, "scheduled_offset_ms": offset})
                resources.append({**receipt, "offset_ms": now()})
            except Exception as err:
                resources.append({"complete": False, "error_type": type(err).__name__, "offset_ms": now()})
            sample += 1

    limits: httpx.Limits = httpx.Limits(max_connections=manifest.runtime.maximum_inflight)
    verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=str(configuration["api_ca_file"])) if configuration.get("api_ca_file") else True
    async with httpx.AsyncClient(base_url=str(configuration.get("api_base_url", "http://127.0.0.1")), timeout=manifest.runtime.request_timeout_seconds,
            trust_env=False, limits=limits, verify=verify) as client:
        async def execute(event: dict[str, object]) -> dict[str, object]:
            if simulated:
                await asyncio.sleep(0)
                return {"status": "success", "simulated": True}
            kind: str = str(event["kind"])
            if kind in ("snapshot", "maintenance"):
                started: float = now()
                receipt = await bind(kind, {"event": kind, "id": event["id"], "scheduled_offset_ms": event["offset_ms"], "campaign_started_at_utc": startedUtc})
                overlaps.append({**receipt, "kind": kind, "started_offset_ms": started, "completed_offset_ms": now()})
                return {"status": "success" if receipt.get("completed") is True else "failed"}
            if kind == "read":
                case = cases[str(event["case_id"])]
                credentials: dict[str, str] = cast(dict[str, str], configuration["credential_files"])
                token: str = _readSecret(Path(credentials[str(case.credential_ref)]))
                started: float = now()
                response: httpx.Response = await client.post(case.request_path, json=payloads[case.id],
                    headers={"Authorization": "Bearer " + token, "X-Request-ID": manifest.id + ":" + str(event["id"])})
                elapsed: float = now() - started
                quality: bool = False
                identity: bool = False
                if response.is_success:
                    assert case.quality_path is not None
                    quality, identity = _verifyQuality(_object(response.json()), _object(_readJson(arguments.manifest.parent / case.quality_path)))
                return {"status": "success" if response.is_success else "failed", "http_status": response.status_code,
                    "full_request_ms": elapsed, "quality_passed": quality, "identity_passed": identity,
                    "response_bytes": len(response.content), "sdk_retries": 0}
            preparedCase: _PreparedCase = prepared[str(event["case_id"])]
            admissionStarted: float = now()
            admission: dict[str, object] = await bind("admission", {"event": "admission", "arrival": event})
            admitted: float = now()
            if admission.get("job_id") != event["job_id"]:
                raise ValueError("Actual admission does not bind the scheduled job identity")
            if admission.get("exclusion_verified") is True and admission.get("exclusion_reason") in ("archived", "superseded"):
                return {"status": "excluded", "exclusion_verified": True, "exclusion_reason": admission["exclusion_reason"], "job_id": event["job_id"]}
            if event["write_kind"] == "retry":
                if admission.get("retry_eligible") is not True:
                    raise ValueError("Explicit retry lacks actual eligible retained-job admission proof")
                credentials = cast(dict[str, str], configuration["credential_files"])
                reference: str = str(_object(configuration["write_credentials"])[str(event["job_id"])])
                token = _readSecret(Path(credentials[reference]))
                admissionStarted = now()
                response = await client.post(f"/v2/jobs/{event['job_id']}/indexing/retry", headers={"Authorization": "Bearer " + token, "X-Request-ID": manifest.id + ":" + str(event["id"])})
                body: dict[str, object] = _object(response.json()) if response.is_success else {}
                return {"status": "success" if response.is_success and body.get("status") == "done" else "failed", "job_state": body.get("status"),
                    "admission_started_offset_ms": admissionStarted, "admitted_offset_ms": now(),
                    "search_ready_offset_ms": now() if body.get("status") == "done" else None, "full_request_ms": now() - admissionStarted}
            if admission.get("admitted") is not True or not isinstance(admission.get("admitted_at_utc"), str):
                raise ValueError("Prepared worker delivery requires an actual committed arrival admission")
            from celery import Celery
            from shared.core.celery_app import celery_app as workerApplication

            def complete() -> dict[str, object]:
                application = Celery("knowhere-mixed-producer", broker=_readSecret(Path(str(configuration["broker_url_file"]))), backend=_readSecret(Path(str(configuration["result_backend_url_file"]))))
                brokerOptions: dict[str, object] = {**dict(workerApplication.conf.broker_transport_options or {}), "retry_on_timeout": False}
                application.conf.update(broker_connection_timeout=manifest.runtime.request_timeout_seconds,
                    broker_connection_retry_on_startup=False, task_publish_retry=False,
                    broker_transport_options=brokerOptions,
                    result_backend_transport_options=dict(workerApplication.conf.result_backend_transport_options or {}),
                    task_queue_max_priority=workerApplication.conf.task_queue_max_priority,
                    task_default_priority=workerApplication.conf.task_default_priority,
                    redis_socket_timeout=manifest.runtime.request_timeout_seconds,
                    redis_socket_connect_timeout=manifest.runtime.request_timeout_seconds)
                try:
                    task = application.send_task("knowhere.probes.complete_prepared", args=[{
                        "campaign_id": manifest.id, "event_id": event["id"], "source_hash": manifest.runtime.source_hash,
                        "job_id": event["job_id"], "owner_id": _object(configuration["job_owners"])[str(event["job_id"])],
                        "prepared_path": _object(configuration["prepared_worker_paths"])[preparedCase.id], "sha256": preparedCase.sha256,
                        "compressed_bytes": preparedCase.compressed_bytes, "uncompressed_bytes": preparedCase.uncompressed_bytes,
                    }], queue=manifest.runtime.worker_queue, serializer="json", retry=False, ignore_result=False,
                        priority=workerApplication.conf.task_default_priority, headers={"campaign_id": manifest.id, "event_id": event["id"]},
                        task_id=str(uuid5(NAMESPACE_URL, manifest.id + ":" + str(event["id"]))))
                    return _object(task.get(timeout=manifest.runtime.request_timeout_seconds, propagate=True))
                finally:
                    application.close()

            result: dict[str, object] = await asyncio.to_thread(complete)
            ready: float | None = now() if result.get("job_state") == "done" else None
            return {**result, "admission_started_offset_ms": admissionStarted, "admitted_offset_ms": admitted,
                "admitted_at_utc": admission["admitted_at_utc"], "search_ready_offset_ms": ready,
                "expected_unit_count": preparedCase.unit_count, "status": "success" if result.get("status") == "success" and result.get("unit_count") == preparedCase.unit_count else "failed"}

        sampler: asyncio.Task[None] = asyncio.create_task(sampleResources())
        try:
            report: dict[str, object] = await support.WorkloadSupport.runOpenLoop(events, execute=execute,
                maximumInflight=manifest.runtime.maximum_inflight, queueCapacity=manifest.runtime.queue_capacity,
                timeoutSeconds=manifest.runtime.request_timeout_seconds, waitUntil=waitUntil, now=now, record=record,
                finishAtMs=runWindowMs if not simulated else None, drainGraceSeconds=manifest.runtime.drain_grace_seconds,
                eventTimeout=lambda event: float(str(bindings[str(event["kind"])]["timeout_seconds"])) + 2
                if not simulated and event["kind"] in ("snapshot", "maintenance") else manifest.runtime.request_timeout_seconds)
            if report.get("cancelled"):
                sampler.cancel()
            try:
                await asyncio.wait_for(sampler, timeout=max(0.001, (runWindowMs - now()) / 1000 + manifest.runtime.drain_grace_seconds))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                resources.append({"complete": False, "reason": "resource-sampling-cancelled-or-deadline", "offset_ms": now()})
        finally:
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)
            eventStream.close()
    isStaging: bool = not simulated and runWindowMs == 3_900_000
    report.update({"mode": "automated-component-simulation" if simulated else "staging-execution" if isStaging else "component-execution",
        "manifest_sha256": sha256(arguments.manifest.read_bytes()).hexdigest(), "campaign_id": manifest.id,
        "source_hash": manifest.runtime.source_hash,
        "actual_staging_campaign": isStaging, "started_at_utc": startedUtc,
        "measured_elapsed_ms": min(now(), 3_600_000) if not simulated else 0,
        "catchup_elapsed_ms": max(0, min(now() - 3_600_000, 300_000)) if not simulated else 0,
        "schedule": schedule, "resource_samples": resources, "overlap_results": overlaps,
        "precondition_receipts": preconditions})
    if not simulated:
        try:
            stageReceipt: dict[str, object] = await bind("stage-export", {"event": "stage-export"})
            telemetryPath: Path = Path(str(stageReceipt["telemetry_path"]))
            telemetryBytes: bytes = telemetryPath.read_bytes()
            if sha256(telemetryBytes).hexdigest() != stageReceipt.get("telemetry_sha256"):
                raise ValueError("Correlated stage export changed from its source-owned receipt")
            telemetry: list[dict[str, object]] = [_object(json.loads(line)) for line in telemetryBytes.splitlines() if line.strip()]
            _joinTelemetry(report, telemetry, campaignId=manifest.id, sourceHash=manifest.runtime.source_hash)
            _joinSnapshotOverlap(report)
            report["telemetry_sha256"] = sha256(telemetryBytes).hexdigest()
        except (OSError, KeyError, ValueError, asyncio.TimeoutError) as err:
            report["telemetry_export_error"] = type(err).__name__
    report["qualification"] = support.WorkloadSupport.evaluateReport(_readJson(arguments.manifest), schedule, report)
    _writeJson(directory / "report.json", report)
    return report


def _main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execution-config", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--issuer-token")
    parser.add_argument("--telemetry", type=Path, help="Later exported actual telemetry for offline reevaluation")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--simulate", action="store_true")
    group.add_argument("--execute", action="store_true")
    group.add_argument("--evaluate-report", type=Path)
    arguments: argparse.Namespace = parser.parse_args()
    support: ModuleType = _loadSupport()
    manifest, schedule, payloads, missing = _validateFiles(arguments.manifest, support)
    if arguments.evaluate_report is not None:
        if arguments.output.exists():
            raise ValueError("Use a new private reevaluation directory")
        report: dict[str, object] = _object(_readJson(arguments.evaluate_report))
        if (report.get("manifest_sha256") != sha256(arguments.manifest.read_bytes()).hexdigest()
                or report.get("campaign_id") != manifest.id or report.get("source_hash") != manifest.runtime.source_hash
                or _object(report.get("schedule"))["schedule_sha256"] != schedule["schedule_sha256"]):
            raise ValueError("Raw report differs from the exact frozen campaign, source or schedule")
        if arguments.telemetry is not None:
            telemetry = [_object(json.loads(line)) for line in arguments.telemetry.read_text().splitlines() if line.strip()]
            _joinTelemetry(report, telemetry, campaignId=manifest.id, sourceHash=manifest.runtime.source_hash)
            _joinSnapshotOverlap(report)
            report["telemetry_sha256"] = sha256(arguments.telemetry.read_bytes()).hexdigest()
        report["raw_report_sha256"] = sha256(arguments.evaluate_report.read_bytes()).hexdigest()
        report["qualification"] = support.WorkloadSupport.evaluateReport(_readJson(arguments.manifest), schedule, report)
        arguments.output.mkdir(mode=0o700)
        _writeJson(arguments.output / "report.json", report)
        print(json.dumps({"mode": "offline-reevaluation", "qualified": _object(report["qualification"])["qualified"], "output": str(arguments.output.resolve())}))
        return 0 if _object(report["qualification"])["qualified"] else 1
    configuration: dict[str, object] = _object(_readJson(arguments.execution_config)) if arguments.execution_config else {}
    if not configuration:
        missing.append("actual-staging-execution-and-probe-bindings")
    requiredBindings: set[str] = {"precondition", "admission", "resource", "snapshot", "maintenance", "stage-export"}
    if configuration and not requiredBindings <= _object(configuration.get("bindings", {})).keys():
        missing.append("actual-admission-precondition-resource-snapshot-maintenance-adapters")
    if configuration and (not configuration.get("issuer") or not configuration.get("operator_manifest_path")):
        missing.append("actual-physical-issuer-and-installation-manifest-binding")
    plan: dict[str, object] = {"mode": "dry-run", "qualified": False, "counts": schedule["counts"],
        "cohorts": schedule["cohort_counts"], "schedule_sha256": schedule["schedule_sha256"],
        "missing_acceptance_inputs": sorted(set(missing)), "prepared_parser_boundary_only": True,
        "worker_include": "scripts.probes.prepared-completion-task", "no_parser_or_model_work": True}
    if not arguments.execute and not arguments.simulate:
        print(json.dumps(plan, indent=2))
        return 0
    if arguments.output.exists():
        raise ValueError("Use a new private campaign output directory")
    if arguments.simulate and manifest.provenance != "automated_test":
        raise ValueError("Accelerated simulation cannot relabel reviewed arrival evidence")
    if arguments.execute:
        if missing:
            raise ValueError("Actual staging execution inputs are incomplete")
        if arguments.execution_config is None or sha256(arguments.execution_config.read_bytes()).hexdigest() != manifest.runtime.configuration_sha256:
            raise ValueError("Staging configuration does not match the frozen campaign checksum")
        receipts: dict[str, object] = _object(configuration.get("receipts", {}))
        for name, expected in (("staging", manifest.runtime.staging_receipt_sha256),
                ("durability", manifest.runtime.durability_receipt_sha256), ("review", manifest.review_receipt_sha256)):
            receiptPath: Path = Path(str(receipts.get(name, "")))
            if not receiptPath.is_file() or sha256(receiptPath.read_bytes()).hexdigest() != expected:
                raise ValueError("A frozen actual staging/durability/review receipt is missing or changed")
            receipt: dict[str, object] = _object(_readJson(receiptPath))
            if receipt.get("mode") != "actual" or receipt.get("status") != "passed" or receipt.get("environment") != "staging" or receipt.get("source_hash") != manifest.runtime.source_hash:
                raise ValueError("Staging/durability/review prerequisites do not qualify this exact source")
        if arguments.approval is None:
            raise ValueError("An explicitly approved bounded staging campaign receipt is required")
        approval: dict[str, object] = _object(_readJson(arguments.approval))
        expires: datetime = datetime.fromisoformat(str(approval.get("expires_at", "")))
        if approval.get("approved") is not True or approval.get("manifest_sha256") != sha256(arguments.manifest.read_bytes()).hexdigest() or approval.get("campaign_id") != manifest.id or not approval.get("user_approval_reference") or not approval.get("bounded_cost_plan_sha256") or expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            raise ValueError("Campaign approval does not bind these exact inputs and duration")
        endpoint = urlsplit(str(configuration["api_base_url"]))
        if endpoint.username or endpoint.password or (endpoint.scheme != "https" and endpoint.hostname not in ("127.0.0.1", "::1")):
            raise ValueError("Use an authenticated approved staging API origin")
    report: dict[str, object] = (_executeApproved(arguments, support, manifest, schedule, payloads, configuration)
        if arguments.execute else asyncio.run(_run(arguments, support, manifest, schedule, payloads, configuration)))
    print(json.dumps({"mode": report["mode"], "offered": report["offered"], "qualified": _object(report["qualification"])["qualified"], "output": str(arguments.output.resolve())}))
    return 0 if arguments.simulate or _object(report["qualification"])["qualified"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except (ValueError, OSError):
        print("Mixed-workload input, approval or campaign execution failed; preserve the private event journal and report.", file=sys.stderr)
        raise SystemExit(2) from None
