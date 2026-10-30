"""Bounded synthetic acceptance against the real isolated Knowhere stack.

Run inside the Worker image with its existing runtime environment. PostgreSQL
observations are read-only. No production settings, service state, or timeouts
are changed. The API key and signed object capabilities are never recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

import httpx
import psycopg2
import pymupdf
import redis
from celery import Celery
from PIL import Image
from psycopg2.extras import RealDictCursor
from psycopg2.extensions import connection as PostgreSQLConnection
from sqlalchemy.engine import make_url


class AcceptanceFailure(RuntimeError):
    """An observed runtime result did not satisfy an acceptance condition."""


def require_mapping(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AcceptanceFailure(f"{description} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def require_mappings(value: object, description: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise AcceptanceFailure(f"{description} must be a JSON array")
    return [require_mapping(item, description) for item in value]


def read_pages(row: dict[str, object]) -> list[int]:
    source: dict[str, object] = require_mapping(row.get("source") or {}, "source")
    metadata: dict[str, object] = require_mapping(
        row.get("metadata") or row.get("chunk_metadata") or {}, "metadata"
    )
    values: object = (
        source.get("page_nums") or row.get("page_nums") or metadata.get("page_nums")
    )
    return [int(str(value)) for value in values] if isinstance(values, list) else []


def sanitize(value: object, secrets: tuple[str, ...] = ()) -> object:
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if re.search(
                    r"url|authorization|password|secret|token|api_key|dsn",
                    str(key),
                    re.I,
                )
                else sanitize(item, secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [sanitize(item, secrets) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        cleaned: str = re.sub(
            r"(?:https?|postgresql[^:]*)://[^\s\"'<>]+", "<address-redacted>", value
        )
        cleaned = re.sub(
            r"Bearer\s+\S+|\bsk-[A-Za-z0-9_-]+", "<credential-redacted>", cleaned
        )
        for secret in secrets:
            if secret:
                cleaned = cleaned.replace(secret, "<credential-redacted>")
        return cleaned[:2000]
    return (
        value
        if value is None or isinstance(value, (int, float, bool))
        else type(value).__name__
    )


@dataclass
class DocumentCase:
    namespace: str
    file_name: str
    markers: list[str]
    original: bytes
    job_id: str = ""
    document_id: str = ""
    revision_id: str = ""
    snapshot_id: str = ""
    directory: dict[str, object] = field(default_factory=dict)
    baseline_inventory: dict[str, dict[str, object]] = field(default_factory=dict)
    evidence: dict[str, object] = field(default_factory=dict)
    submitted_at: float = 0


class AcceptanceRunner:
    def __init__(self, arguments: argparse.Namespace) -> None:
        self.started_at: float = time.monotonic()
        self.deadline: float = self.started_at + arguments.timeout_seconds
        self.output: Path = arguments.output
        self.worker_health_evidence: Path | None = arguments.worker_health_evidence
        self.stage: str = "initialize"
        self.api_base: str = arguments.api_base.rstrip("/")
        authentication: dict[str, object] = require_mapping(
            json.loads(arguments.auth_file.read_text()), "auth file"
        )
        self.api_key: str = str(authentication.get("api_key") or "")
        self.user_id: str = str(authentication.get("user_id") or "")
        if not self.api_key or not self.user_id:
            raise AcceptanceFailure("auth file requires api_key and user_id")
        self.api: httpx.Client = httpx.Client(
            headers={"Authorization": f"Bearer {self.api_key}"},
            trust_env=False,
            follow_redirects=False,
        )
        self.objects: httpx.Client = httpx.Client(
            trust_env=False, follow_redirects=True
        )
        self.database = self.connect_database()
        self.run_id: str = uuid4().hex[:16]
        self.cases: list[DocumentCase] = self.create_cases(arguments.fixture)
        self.report: dict[str, object] = {
            "run_id": self.run_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "fixture_kind": "synthetic native three-page PDF; unique markers per namespace/page",
            "scope": "real HTTP + PostgreSQL + Redis + S3 + Celery integration; no capacity claim",
            "status": "running",
            "checks": [],
            "documents": [case.evidence for case in self.cases],
            "worker_observation": {
                "responding_workers": [],
                "active_parse_correlations": [],
                "both_workers_processed_documents": "not established",
            },
        }
        self.write_report()

    def connect_database(self) -> PostgreSQLConnection:
        configured: str = os.getenv("DATABASE_URL", "")
        if not configured:
            raise AcceptanceFailure(
                "DATABASE_URL is required from the existing Worker environment"
            )
        address = make_url(configured)
        connection = psycopg2.connect(
            host=address.host,
            port=address.port or 5432,
            user=address.username,
            password=address.password,
            dbname=address.database,
            connect_timeout=5,
            sslmode=os.getenv("DB_SSL_MODE", "disable"),
            options="-c statement_timeout=5000 -c default_transaction_read_only=on",
        )
        connection.autocommit = True
        return connection

    def create_cases(self, fixture: Path) -> list[DocumentCase]:
        cases: list[DocumentCase] = []
        for label in ("alpha", "bravo"):
            markers: list[str] = [
                f"Probe{self.run_id}{label}{word}" for word in ("one", "two", "three")
            ]
            with pymupdf.open(fixture) as document:
                if len(document) != 3:
                    raise AcceptanceFailure(
                        "Expected the synthetic three-page native fixture"
                    )
                for index, page in enumerate(document):
                    page.insert_text((48, 420), markers[index], fontsize=10)
                original: bytes = document.tobytes(garbage=4, deflate=True)
            namespace: str = f"progressive-e2e-{self.run_id}-{label}"
            case: DocumentCase = DocumentCase(
                namespace, f"{label}-{self.run_id}.pdf", markers, original
            )
            case.evidence = {
                "namespace": namespace,
                "file_name": case.file_name,
                "markers": markers,
                "input_sha256": hashlib.sha256(original).hexdigest(),
                "input_bytes": len(original),
                "job_status_history": [],
            }
            cases.append(case)
        return cases

    def remaining(self) -> float:
        remaining: float = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AcceptanceFailure(f"Run deadline exceeded during {self.stage}")
        return remaining

    def set_stage(self, name: str) -> None:
        self.stage = name
        self.report["current_stage"] = name
        self.write_report()
        print(
            json.dumps(
                {
                    "stage": name,
                    "elapsed_seconds": round(time.monotonic() - self.started_at, 2),
                }
            ),
            flush=True,
        )

    def check(self, name: str, condition: bool, detail: object) -> None:
        checks: object = self.report["checks"]
        if isinstance(checks, list):
            checks.append(
                {
                    "name": name,
                    "passed": condition,
                    "detail": sanitize(detail, (self.api_key,)),
                }
            )
        self.write_report()
        if not condition:
            raise AcceptanceFailure(
                f"{name}: {json.dumps(sanitize(detail, (self.api_key,)))}"
            )

    def write_report(self) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path = self.output.with_suffix(".partial")
        descriptor: int = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w") as stream:
            json.dump(sanitize(self.report, (self.api_key,)), stream, indent=2)
            stream.write("\n")
        temporary.replace(self.output)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        params: dict[str, object] | None = None,
        expected_status: int = 200,
    ) -> dict[str, object]:
        timeout: float = min(30.0, self.remaining())
        try:
            response: httpx.Response = self.api.request(
                method,
                f"{self.api_base}{path}",
                json=payload,
                params=params,
                timeout=timeout,
            )
        except httpx.RequestError as error:
            raise AcceptanceFailure(
                f"{method} {path}: transport {type(error).__name__}"
            ) from None
        if response.status_code != expected_status:
            try:
                body: object = response.json()
            except ValueError:
                body = response.text[:1200]
            raise AcceptanceFailure(
                f"{method} {path}: HTTP {response.status_code}, expected {expected_status}; "
                f"response={json.dumps(sanitize(body, (self.api_key,)))}"
            )
        return require_mapping(response.json(), f"{method} {path}")

    def read_database(
        self, query: str, values: tuple[object, ...] = ()
    ) -> list[dict[str, object]]:
        self.remaining()
        with self.database.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(query, values)
            return [dict(row) for row in cursor.fetchall()]

    def read_scopes(self, case: DocumentCase) -> list[dict[str, object]]:
        return self.read_database(
            "SELECT scope_id,page_start,page_end,state,attempts,claim_generation,last_hit_at,updated_at "
            "FROM document_scope_enrichments WHERE document_id=%s AND job_result_id=%s ORDER BY scope_id",
            (case.document_id, case.revision_id),
        )

    def untouched(self, rows: list[dict[str, object]]) -> bool:
        return all(
            row["state"] == "coarse"
            and row["attempts"] == 0
            and row["last_hit_at"] is None
            for row in rows
        )

    def observe_workers(self, *, require_two: bool = False) -> None:
        from shared.core.celery_app import get_celery_app

        application: Celery = get_celery_app()
        observation: dict[str, object] = require_mapping(
            self.report["worker_observation"], "worker observation"
        )
        observation["remote_control_enabled"] = bool(
            application.conf.worker_enable_remote_control
        )
        transport: dict[str, object] = require_mapping(
            application.conf.broker_transport_options, "broker transport options"
        )
        observation["broker_global_keyprefix"] = transport.get("global_keyprefix")
        self.report["worker_observation"] = observation
        if require_two:
            self.verify_worker_health_evidence()
            observation = require_mapping(
                self.report["worker_observation"], "worker observation"
            )
        if not application.conf.worker_enable_remote_control:
            observation["processing_attribution"] = (
                "unavailable: remote control is intentionally disabled; correlate worker logs separately"
            )
            observation["control_observation"] = "not attempted by configured policy"
            self.report["worker_observation"] = observation
            self.write_report()
            return
        inspection = application.control.inspect(timeout=min(2.0, self.remaining()))
        if require_two:
            responses: dict[str, object] = inspection.ping() or {}
            observation["responding_workers"] = sorted(responses)
            self.check(
                "two worker processes respond when remote control is enabled",
                len(responses) == 2,
                sorted(responses),
            )
        else:
            active: dict[str, object] = inspection.active() or {}
            correlations: list[dict[str, object]] = require_mappings(
                observation["active_parse_correlations"], "correlations"
            )
            for worker, tasks in active.items():
                if not isinstance(tasks, list):
                    continue
                for raw_task in tasks:
                    task: dict[str, object] = require_mapping(raw_task, "active task")
                    if not str(task.get("name") or "").endswith(".parse_task"):
                        continue
                    arguments: object = task.get("args")
                    if not isinstance(arguments, (list, tuple)):
                        continue
                    for case in self.cases:
                        if case.job_id and case.job_id in arguments:
                            item: dict[str, object] = {
                                "worker": worker,
                                "job_id": case.job_id,
                                "task_id": task.get("id"),
                            }
                            if item not in correlations:
                                correlations.append(item)
            observation["active_parse_correlations"] = correlations
            processors: set[str] = {str(item["worker"]) for item in correlations}
            if len(processors) == 2:
                observation["both_workers_processed_documents"] = (
                    "active parse tasks correlated on both workers; completion ownership requires logs"
                )
        self.report["worker_observation"] = observation
        self.write_report()

    def verify_worker_health_evidence(self) -> None:
        if self.worker_health_evidence is None:
            raise AcceptanceFailure(
                "--worker-health-evidence is required: root must capture Docker health "
                "and execute shared.services.worker_health.assert_worker_healthy in each worker"
            )
        proof: dict[str, object] = require_mapping(
            json.loads(self.worker_health_evidence.read_text(encoding="utf-8-sig")),
            "worker health evidence",
        )
        captured_at: datetime = datetime.fromisoformat(
            str(proof.get("capturedAt") or "").replace("Z", "+00:00")
        )
        self.check(
            "worker health capture includes timezone",
            captured_at.tzinfo is not None,
            {"capturedAt": proof.get("capturedAt")},
        )
        age_seconds: float = (datetime.now(timezone.utc) - captured_at).total_seconds()
        self.check(
            "initial two-worker health evidence is fresh",
            0 <= age_seconds <= 60,
            {"age_seconds": round(age_seconds, 3), "maximum_age_seconds": 60},
        )
        workers: list[dict[str, object]] = require_mappings(
            proof.get("workers"), "worker health records"
        )
        identifiers: set[str] = {
            str(worker.get("containerId") or "") for worker in workers
        }
        services: set[str] = {str(worker.get("service") or "") for worker in workers}
        self.check(
            "two distinct worker containers have captured health",
            len(workers) == 2
            and len(identifiers) == 2
            and all(
                re.fullmatch(r"[0-9a-f]{64}", identifier) for identifier in identifiers
            )
            and services == {"worker-a", "worker-b"},
            workers,
        )
        for worker in workers:
            service: str = str(worker["service"])
            self.check(
                f"{service}: Docker health and actual heartbeat reader pass",
                worker.get("dockerHealth") == "healthy"
                and worker.get("assertWorkerHealthyPassed") is True,
                worker,
            )
            self.check(
                f"{service}: production worker resource/concurrency baseline preserved",
                worker.get("nanoCpus") == 2_000_000_000
                and worker.get("memoryBytes") == 4_294_967_296
                and str(worker.get("workerConcurrency")) == "10",
                worker,
            )
        observation: dict[str, object] = require_mapping(
            self.report["worker_observation"], "worker observation"
        )
        observation["health_evidence"] = proof
        observation["health_evidence_validated_once_at_start"] = True
        self.report["worker_observation"] = observation
        self.write_report()

    def verify_infrastructure(self) -> None:
        self.set_stage("infrastructure_readiness")
        health: dict[str, object] = self.request("GET", "/health")
        self.check("API health", health.get("status") == "healthy", health)
        role: list[dict[str, object]] = self.read_database(
            "SELECT rolname,rolsuper,rolbypassrls,current_setting('transaction_read_only') AS read_only "
            "FROM pg_roles WHERE rolname=current_user"
        )
        self.check(
            "read-only observer uses runtime role",
            len(role) == 1
            and role[0]["rolsuper"] is False
            and role[0]["rolbypassrls"] is False
            and role[0]["read_only"] == "on",
            role,
        )
        client = redis.Redis.from_url(
            os.environ["CELERY_REDIS_URL"], socket_timeout=5, socket_connect_timeout=5
        )
        try:
            self.check(
                "isolated Redis broker responds", client.ping() is True, {"ping": True}
            )
        finally:
            client.close()
        self.observe_workers(require_two=True)

    def submit_cases(self) -> None:
        self.set_stage("submit_two_synthetic_documents")
        for case in self.cases:
            case.submitted_at = time.monotonic()
            response: dict[str, object] = self.request(
                "POST",
                "/v2/jobs",
                payload={
                    "source_type": "file",
                    "file_name": case.file_name,
                    "namespace": case.namespace,
                    "ingestion_mode": "progressive",
                },
            )
            case.job_id = str(response.get("job_id") or "")
            case.document_id = str(response.get("document_id") or "")
            case.evidence.update(
                {"job_id": case.job_id, "document_id": case.document_id}
            )
            self.check(
                f"{case.namespace}: job accepted",
                bool(case.job_id) and response.get("status") == "waiting-file",
                {"job_id": case.job_id, "status": response.get("status")},
            )
            upload_address: str = str(response.get("upload_url") or "")
            headers: dict[str, object] = require_mapping(
                response.get("upload_headers") or {}, "upload headers"
            )
            try:
                uploaded: httpx.Response = self.objects.put(
                    upload_address,
                    content=case.original,
                    headers={key: str(value) for key, value in headers.items()},
                    timeout=min(30, self.remaining()),
                )
            except httpx.RequestError as error:
                raise AcceptanceFailure(
                    f"Signed PUT for {case.job_id}: transport {type(error).__name__}"
                ) from None
            self.check(
                f"{case.namespace}: original uploaded",
                uploaded.status_code in {200, 201, 204},
                {"http_status": uploaded.status_code, "bytes": len(case.original)},
            )
            self.request("POST", f"/v2/jobs/{case.job_id}/confirm-upload", payload={})

    def poll_baselines(self) -> None:
        self.set_stage("wait_first_readable_baselines")
        pending: list[DocumentCase] = list(self.cases)
        iteration: int = 0
        while pending:
            for case in list(pending):
                job: dict[str, object] = self.request("GET", f"/v2/jobs/{case.job_id}")
                status: str = str(job.get("status"))
                history: object = case.evidence["job_status_history"]
                if isinstance(history, list) and (
                    not history
                    or require_mapping(history[-1], "history")["status"] != status
                ):
                    history.append(
                        {
                            "status": status,
                            "after_submit_seconds": round(
                                time.monotonic() - case.submitted_at, 3
                            ),
                        }
                    )
                if status == "failed":
                    case.evidence["job_error"] = sanitize(
                        job.get("error"), (self.api_key,)
                    )
                    raise AcceptanceFailure(
                        f"Ingestion failed for {case.job_id}: {json.dumps(sanitize(job.get('error'), (self.api_key,)))}"
                    )
                if status != "done":
                    continue
                snapshots: list[dict[str, object]] = require_mappings(
                    job.get("snapshots"), "published snapshots"
                )
                baseline: dict[str, object] = next(
                    (item for item in snapshots if item.get("stage") == "baseline"), {}
                )
                case.snapshot_id = str(baseline.get("snapshot_id") or "")
                case.revision_id = str(job.get("job_result_id") or "")
                case.document_id = str(job.get("document_id") or case.document_id)
                self.check(
                    f"{case.namespace}: baseline publication identity",
                    bool(case.snapshot_id and case.revision_id and case.document_id),
                    {
                        "snapshot_id": case.snapshot_id,
                        "revision_id": case.revision_id,
                        "document_id": case.document_id,
                    },
                )
                case.evidence.update(
                    {
                        "snapshot_id": case.snapshot_id,
                        "revision_id": case.revision_id,
                        "document_id": case.document_id,
                        "baseline_observed_seconds": round(
                            time.monotonic() - case.submitted_at, 3
                        ),
                        "enhancement_at_baseline": job.get("enhancement"),
                        "page_count": job.get("page_count"),
                    }
                )
                pending.remove(case)
                self.verify_baseline(case)
            self.write_report()
            if pending:
                if iteration % 3 == 0:
                    self.observe_workers()
                time.sleep(min(2.0, self.remaining()))
                iteration += 1

    def read_snapshot_objects(
        self, case: DocumentCase, paths: list[str]
    ) -> dict[str, bytes]:
        capabilities: dict[str, object] = self.request(
            "POST",
            f"/v2/jobs/{case.job_id}/snapshots/{case.snapshot_id}/objects/access",
            payload={"paths": paths},
        )
        objects: dict[str, bytes] = {}
        for capability in require_mappings(
            capabilities.get("objects"), "object capabilities"
        ):
            path: str = str(capability.get("path") or "")
            try:
                response: httpx.Response = self.objects.get(
                    str(capability.get("download_url") or ""),
                    timeout=min(30, self.remaining()),
                )
            except httpx.RequestError as error:
                raise AcceptanceFailure(
                    f"Read immutable object {path}: transport {type(error).__name__}"
                ) from None
            self.check(
                f"{case.namespace}: object readable {path}",
                response.status_code == 200,
                {"http_status": response.status_code, "bytes": len(response.content)},
            )
            inventory: dict[str, object] = case.baseline_inventory[path]
            self.check(
                f"{case.namespace}: object checksum {path}",
                hashlib.sha256(response.content).hexdigest()
                == inventory.get("checksum_sha256")
                and len(response.content) == inventory.get("size_bytes"),
                {
                    "size_bytes": len(response.content),
                    "sha256": hashlib.sha256(response.content).hexdigest(),
                },
            )
            objects[path] = response.content
        self.check(
            f"{case.namespace}: all requested objects returned",
            set(objects) == set(paths),
            {"paths": sorted(objects)},
        )
        return objects

    def verify_baseline(self, case: DocumentCase) -> None:
        inventory: dict[str, object] = self.request(
            "GET", f"/v2/jobs/{case.job_id}/snapshots/{case.snapshot_id}/readable"
        )
        case.baseline_inventory = {
            str(item["path"]): item
            for item in require_mappings(inventory.get("objects"), "inventory")
        }
        core: dict[str, bytes] = self.read_snapshot_objects(
            case, ["chunks.json", "source.pdf", "manifest.json", "doc_nav.json"]
        )
        chunks: list[dict[str, object]] = require_mappings(
            require_mapping(json.loads(core["chunks.json"]), "baseline chunks").get(
                "chunks"
            ),
            "baseline chunks",
        )
        page_chunks: list[dict[str, object]] = [
            row
            for row in chunks
            if (row.get("type") or row.get("chunk_type")) == "page"
        ]
        self.check(
            f"{case.namespace}: three baseline page chunks",
            len(page_chunks) == 3,
            {
                "page_chunk_count": len(page_chunks),
                "pages": [read_pages(row) for row in page_chunks],
            },
        )
        assets: dict[int, dict[str, object]] = {}
        for page_number in range(1, 4):
            matching: list[dict[str, object]] = [
                row for row in page_chunks if read_pages(row) == [page_number]
            ]
            self.check(
                f"{case.namespace}: original page {page_number} identity/text",
                len(matching) == 1
                and case.markers[page_number - 1]
                in str(matching[0].get("content") or ""),
                {
                    "matching_page_chunks": len(matching),
                    "marker_present": bool(
                        matching
                        and case.markers[page_number - 1]
                        in str(matching[0].get("content") or "")
                    ),
                },
            )
            metadata: dict[str, object] = require_mapping(
                matching[0].get("metadata"), "baseline metadata"
            )
            for asset in require_mappings(
                metadata.get("page_assets"), "eager page assets"
            ):
                assets[int(str(asset.get("page_num")))] = asset
        self.check(
            f"{case.namespace}: three eager page assets",
            sorted(assets) == [1, 2, 3],
            {"asset_pages": sorted(assets)},
        )
        paths: list[str] = [str(assets[number]["artifact_ref"]) for number in (1, 2, 3)]
        self.check(
            f"{case.namespace}: eager assets included in baseline inventory",
            set(paths) <= set(case.baseline_inventory),
            {"asset_paths": paths},
        )
        downloaded: dict[str, bytes] = self.read_snapshot_objects(case, paths)
        summaries: list[dict[str, object]] = []
        for page_number in (1, 2, 3):
            path: str = paths[page_number - 1]
            with Image.open(io.BytesIO(downloaded[path])) as image:
                image.load()
                self.check(
                    f"{case.namespace}: page {page_number} image decodes",
                    image.width > 0 and image.height > 0,
                    {
                        "width": image.width,
                        "height": image.height,
                        "format": image.format,
                    },
                )
                summaries.append(
                    {
                        "page_num": page_number,
                        "path": path,
                        "width": image.width,
                        "height": image.height,
                    }
                )
        self.check(
            f"{case.namespace}: source PDF preserves original bytes",
            core["source.pdf"] == case.original,
            {"sha256": hashlib.sha256(core["source.pdf"]).hexdigest()},
        )
        with pymupdf.open(stream=core["source.pdf"], filetype="pdf") as source:
            self.check(
                f"{case.namespace}: source PDF original page identity",
                len(source) == 3
                and all(
                    case.markers[index] in page.get_text()
                    for index, page in enumerate(source)
                ),
                {"pages": len(source)},
            )
        case.evidence["baseline_assets"] = summaries
        case.evidence["baseline_objects"] = [
            {
                "path": path,
                "checksum_sha256": item.get("checksum_sha256"),
                "size_bytes": item.get("size_bytes"),
            }
            for path, item in case.baseline_inventory.items()
        ]
        case.evidence["baseline_read_verified_seconds"] = round(
            time.monotonic() - case.submitted_at, 3
        )
        self.write_report()

    def wait_directories(self) -> None:
        self.set_stage("wait_stable_directories_without_fine")
        pending: list[DocumentCase] = list(self.cases)
        while pending:
            for case in list(pending):
                directory: dict[str, object] = self.request(
                    "GET",
                    f"/v2/documents/{case.document_id}/structure",
                    params={
                        "namespace": case.namespace,
                        "job_result_id": case.revision_id,
                    },
                )
                if directory.get("directory_version") != 1:
                    continue
                case.directory = directory
                case.evidence["directory_observed_seconds"] = round(
                    time.monotonic() - case.submitted_at, 3
                )
                case.evidence["directory"] = directory
                self.check(
                    f"{case.namespace}: directory identity",
                    directory.get("document_id") == case.document_id
                    and directory.get("job_result_id") == case.revision_id
                    and directory.get("namespace") == case.namespace
                    and bool(directory.get("directory_committed_at")),
                    directory,
                )
                pending.remove(case)
            if pending:
                time.sleep(min(2.0, self.remaining()))
        for case in self.cases:
            initial_scopes: list[dict[str, object]] = self.read_scopes(case)
            self.check(
                f"{case.namespace}: Fine remains untouched before reads",
                bool(initial_scopes) and self.untouched(initial_scopes),
                initial_scopes,
            )
            for _ in range(3):
                repeated: dict[str, object] = self.request(
                    "GET",
                    f"/v2/documents/{case.document_id}/structure",
                    params={
                        "namespace": case.namespace,
                        "job_result_id": case.revision_id,
                    },
                )
                self.check(
                    f"{case.namespace}: directory remains immutable",
                    repeated == case.directory,
                    {
                        "directory_version": repeated.get("directory_version"),
                        "section_count": len(
                            require_mappings(
                                repeated.get("sections"), "directory sections"
                            )
                        ),
                    },
                )
            chunks: dict[str, object] = self.request(
                "GET",
                f"/v2/documents/{case.document_id}/chunks",
                params={"job_result_id": case.revision_id, "page_size": 200},
            )
            rows: list[dict[str, object]] = require_mappings(
                chunks.get("chunks"), "live chunks"
            )
            self.check(
                f"{case.namespace}: live chunks retain original page identities",
                {number for row in rows for number in read_pages(row)} == {1, 2, 3},
                {"pages": [read_pages(row) for row in rows]},
            )
            observed_scopes: list[dict[str, object]] = self.read_scopes(case)
            self.check(
                f"{case.namespace}: directory/chunk reads do not trigger Fine",
                self.untouched(observed_scopes),
                observed_scopes,
            )
            case.evidence["scopes_before_retrieval"] = observed_scopes

    def retrieve(
        self, namespace: str, document_ids: list[str], marker: str
    ) -> dict[str, object]:
        return self.request(
            "POST",
            "/v2/retrieval/query",
            payload={
                "namespace": namespace,
                "include_document_ids": document_ids,
                "query": marker,
                "top_k": 1,
                "use_agentic": False,
                "rerank": False,
                "chunk_types": ["page"],
            },
        )

    def verify_isolation_and_promotion(self) -> None:
        self.set_stage("namespace_fencing_and_read_driven_fine")
        alpha, bravo = self.cases
        for case, foreign in ((alpha, bravo), (bravo, alpha)):
            listed: dict[str, object] = self.request(
                "GET", "/v2/documents", params={"namespace": case.namespace}
            )
            identifiers: set[str] = {
                str(item.get("document_id"))
                for item in require_mappings(
                    listed.get("documents"), "namespace documents"
                )
            }
            self.check(
                f"{case.namespace}: namespace document isolation",
                identifiers == {case.document_id},
                {"document_ids": sorted(identifiers)},
            )
            self.request(
                "GET",
                f"/v2/documents/{foreign.document_id}/structure",
                params={"namespace": case.namespace},
                expected_status=404,
            )
            self.check(
                f"{case.namespace}: foreign directory namespace rejected",
                True,
                {"http_status": 404},
            )
            rejected: dict[str, object] = self.retrieve(
                case.namespace, [foreign.document_id], foreign.markers[0]
            )
            self.check(
                f"{case.namespace}: foreign document allowlist cannot cross namespace",
                require_mappings(rejected.get("results"), "foreign results") == [],
                {
                    "result_count": len(
                        require_mappings(rejected.get("results"), "foreign results")
                    ),
                    "router_used": rejected.get("router_used"),
                },
            )
            empty: dict[str, object] = self.retrieve(
                case.namespace, [], case.markers[0]
            )
            self.check(
                f"{case.namespace}: empty document allowlist returns no results",
                require_mappings(empty.get("results"), "empty results") == [],
                {
                    "result_count": len(
                        require_mappings(empty.get("results"), "empty results")
                    )
                },
            )
        self.check(
            "negative retrieval controls do not promote either namespace",
            all(self.untouched(self.read_scopes(case)) for case in self.cases),
            {case.namespace: self.read_scopes(case) for case in self.cases},
        )
        for case in self.cases:
            before: list[dict[str, object]] = self.read_scopes(case)
            result: dict[str, object] = self.retrieve(
                case.namespace, [case.document_id], case.markers[0]
            )
            rows: list[dict[str, object]] = require_mappings(
                result.get("results"), "classic results"
            )
            self.check(
                f"{case.namespace}: classic top_k=1 returns selected page",
                len(rows) == 1
                and result.get("router_used") == "classic_topk"
                and read_pages(rows[0]) == [1]
                and case.markers[0] in str(rows[0].get("content") or ""),
                {
                    "result_count": len(rows),
                    "pages": [read_pages(row) for row in rows],
                    "router_used": result.get("router_used"),
                },
            )
            source: dict[str, object] = require_mapping(
                rows[0].get("source"), "retrieval source"
            )
            self.check(
                f"{case.namespace}: retrieved original document/revision identity",
                source.get("document_id") == case.document_id
                and str(rows[0].get("job_result_id") or source.get("job_result_id"))
                == case.revision_id,
                source,
            )
            metadata: dict[str, object] = require_mapping(
                rows[0].get("metadata"), "retrieval metadata"
            )
            scope_id: str = str(metadata.get("scope_id") or "")
            expected: set[str] = (
                {scope_id}
                if scope_id
                else {
                    str(row["scope_id"])
                    for row in before
                    if int(str(row["page_start"])) <= 1 <= int(str(row["page_end"]))
                }
            )
            after: list[dict[str, object]] = self.read_scopes(case)
            promoted: list[dict[str, object]] = [
                row for row in after if str(row["scope_id"]) in expected
            ]
            untouched: list[dict[str, object]] = [
                row for row in after if str(row["scope_id"]) not in expected
            ]
            self.check(
                f"{case.namespace}: returned scope was promoted",
                bool(expected)
                and len(promoted) == len(expected)
                and all(
                    row["last_hit_at"] is not None
                    and row["state"] in {"queued", "running", "refined", "failed"}
                    for row in promoted
                ),
                {"expected_scope_ids": sorted(expected), "observed": promoted},
            )
            self.check(
                f"{case.namespace}: unreturned scopes remain coarse",
                self.untouched(untouched),
                {"unreturned_scope_count": len(untouched), "observed": untouched},
            )
            repeated: dict[str, object] = self.request(
                "GET",
                f"/v2/documents/{case.document_id}/structure",
                params={"namespace": case.namespace},
            )
            self.check(
                f"{case.namespace}: Fine promotion leaves frozen directory unchanged",
                repeated == case.directory,
                {"directory_version": repeated.get("directory_version")},
            )
            case.evidence["retrieval"] = {
                "router_used": result.get("router_used"),
                "pages": read_pages(rows[0]),
                "chunk_id": rows[0].get("chunk_id"),
                "expected_promoted_scope_ids": sorted(expected),
                "scopes_after": after,
            }
            if case is alpha:
                self.check(
                    "reading alpha does not promote bravo",
                    self.untouched(self.read_scopes(bravo)),
                    self.read_scopes(bravo),
                )
        self.set_stage("immutable_baseline_after_read_promotion")
        for case in self.cases:
            self.read_snapshot_objects(case, ["chunks.json", "source.pdf"])
            timing: list[dict[str, object]] = self.read_database(
                "SELECT job_metadata->'stages'->'timing_ms' AS timing_ms FROM jobs WHERE job_id=%s AND user_id=%s",
                (case.job_id, self.user_id),
            )
            case.evidence["worker_stage_timing_ms"] = (
                timing[0].get("timing_ms") if timing else None
            )

    def execute(self, *, stop_after_directory: bool = False) -> int:
        self.verify_infrastructure()
        self.submit_cases()
        self.poll_baselines()
        self.wait_directories()
        if not stop_after_directory:
            self.verify_isolation_and_promotion()
        self.report["status"] = "prepared" if stop_after_directory else "passed"
        self.report["remaining_acceptance"] = [
            "Hipo browser/Ask integration",
            "delayed/failed Fine fault injection",
            "worker loss and recovery",
            "real 188/407-page originals",
            "capacity measurement",
        ]
        return 0

    def finish(self) -> None:
        self.report["elapsed_seconds"] = round(time.monotonic() - self.started_at, 3)
        self.write_report()
        self.api.close()
        self.objects.close()
        self.database.close()
        checks: object = self.report.get("checks")
        print(
            json.dumps(
                {
                    "status": self.report.get("status"),
                    "stage": self.stage,
                    "checks": len(checks) if isinstance(checks, list) else 0,
                    "elapsed_seconds": self.report["elapsed_seconds"],
                    "evidence_file": str(self.output),
                    "failure": self.report.get("failure"),
                }
            ),
            flush=True,
        )


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://knowhere-api:5005")
    parser.add_argument("--auth-file", type=Path, default=Path("/artifacts/auth.json"))
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--worker-health-evidence", type=Path)
    parser.add_argument("--stop-after-directory", action="store_true")
    arguments: argparse.Namespace = parser.parse_args()
    if not 30 <= arguments.timeout_seconds <= 600:
        parser.error("--timeout-seconds must be between 30 and 600")
    if arguments.output.exists():
        parser.error("--output must be a fresh evidence path")
    runner: AcceptanceRunner | None = None

    def stop_at_deadline(_number: int, _frame: object) -> None:
        raise AcceptanceFailure("Hard run deadline exceeded")

    previous_handler: Callable | int | None = signal.signal(
        signal.SIGALRM, stop_at_deadline
    )
    signal.alarm(arguments.timeout_seconds)
    try:
        runner = AcceptanceRunner(arguments)
        return runner.execute(stop_after_directory=arguments.stop_after_directory)
    except Exception as error:
        if runner is None:
            # Initialization errors can contain connection credentials, so expose
            # only the type plus an explicitly authored acceptance message.
            failure: object = {
                "type": type(error).__name__,
                "message": str(error)
                if isinstance(error, AcceptanceFailure)
                else "Initialization failed before runtime observations; check required mounts/environment privately",
            }
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(
                os.open(arguments.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                "w",
            ) as stream:
                json.dump(
                    {
                        "status": "failed",
                        "stage": "initialize",
                        "failure": sanitize(failure),
                    },
                    stream,
                    indent=2,
                )
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "stage": "initialize",
                        "failure": sanitize(failure),
                    }
                ),
                flush=True,
            )
        else:
            runner.report["status"] = "failed"
            frames: list[dict[str, object]] = [
                {
                    "file": Path(frame.filename).name,
                    "line": frame.lineno,
                    "function": frame.name,
                }
                for frame in traceback.extract_tb(error.__traceback__)[-4:]
            ]
            runner.report["failure"] = sanitize(
                {
                    "type": type(error).__name__,
                    "message": str(error)
                    if isinstance(
                        error,
                        (
                            AcceptanceFailure,
                            KeyError,
                            ValueError,
                            TypeError,
                            IndexError,
                        ),
                    )
                    else "Unexpected runtime exception; no raw exception text emitted",
                    "database_sqlstate": getattr(error, "pgcode", None),
                    "database_message": getattr(
                        getattr(error, "diag", None), "message_primary", None
                    ),
                    "location": frames,
                },
                (runner.api_key,),
            )
        return 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
        if runner is not None:
            runner.finish()


if __name__ == "__main__":
    sys.exit(main())
