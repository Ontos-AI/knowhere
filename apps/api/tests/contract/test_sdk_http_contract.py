"""Built standalone SDKs exercise actual job HTTP routes without parser providers.

Run through scripts/probes/verify-sdk-http-contract.py to require external SDK
artifacts. Routine API-only CI skips this optional cross-repository campaign.
Only authentication, object delivery and native I/O boundaries are substituted;
job projections, admission, SQL phases, retry service and error handlers are real.
"""

from __future__ import annotations
from typing import TypeVar, cast
import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import hmac
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Thread
import time
from uuid import uuid4
import zipfile
import pytest
from fastapi import FastAPI, HTTPException, Request, Response
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session
from shared.testing.contract_runtime import (
    PostgreSQLProcess,
    clear_application_modules,
    configure_contract_environment,
    get_contract_database_url,
    prepare_contract_storage,
)

_ROOT: Path = Path(__file__).resolve().parents[4]
_MODES: tuple[str, ...] = (
    "running",
    "completed",
    "unavailable",
    "invalid",
    "timeout",
    "conflict",
    "response-lost",
)


def makeArchive(jobId: str) -> bytes:
    manifest: dict[str, object] = {
        "version": "2.0",
        "job_id": jobId,
        "source_file_name": "synthetic.pdf",
        "processing_date": "2026-11-05T00:00:00Z",
        "statistics": {
            "total_chunks": 1,
            "text_chunks": 1,
            "image_chunks": 0,
            "table_chunks": 0,
            "total_pages": 1,
        },
        "files": {
            "chunks": "chunks.json",
            "markdown": "full.md",
            "images": [],
            "tables": [],
        },
    }
    chunk: dict[str, object] = {
        "chunk_id": "synthetic-evidence",
        "type": "text",
        "content": "Synthetic retained evidence",
        "path": "synthetic.pdf/Section",
        "metadata": {},
    }
    output: BytesIO = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("chunks.json", json.dumps({"chunks": [chunk]}))
        archive.writestr("full.md", "Synthetic retained evidence")
    return output.getvalue()


@pytest.fixture
def sdkHttpRuntime(
    monkeypatch: pytest.MonkeyPatch,
    postgresql_proc: PostgreSQLProcess,
    request: pytest.FixtureRequest,
) -> Iterator[dict[str, object]]:
    configure_contract_environment(monkeypatch, postgresql_proc)
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    clear_application_modules()
    asyncio.run(prepare_contract_storage())
    import httpx
    import uvicorn
    from app.api.dependencies.auth import require_write_permission
    from app.api.dependencies.current_user import with_current_user
    from app.api.v2.routes.jobs import router
    from app.core.exception_handlers import setup_exception_handlers
    from app.services.rate_limit.data_structures import CurrentUser
    from shared.core.config import settings
    from shared.core.database import get_db
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.lexicalnamespace import LexicalNamespaceIndex
    from shared.models.database.user import User
    from shared.models.database.webhook import WebhookEvent
    from shared.services.retrieval.lexical.asyncdatabase import AsyncLexicalDatabase
    from shared.services.retrieval.lexical.asynccoordination import (
        AsyncLexicalCoordination,
    )
    from shared.services.retrieval.lexical.asyncnative import (
        AsyncOpenSearchLexicalIndex,
    )
    from shared.services.retrieval.lexical.copy_dispatch import LegacyCopyDispatch
    from shared.services.retrieval.lexical.error import LexicalIndexError
    from shared.services.retrieval.lexical.parsing import ParsedRevisionFinalizer
    from shared.services.retrieval.lexical.scope import LexicalScope
    from shared.services.retrieval.lexical.settlement import IndexingSettlement
    from shared.services.storage.job_file_storage import JobFileStorage
    from shared.services.webhook import qstash_publisher
    from shared.services.webhook.qstash_payload import QStashPayloadEnricher
    from shared.services.webhook.signing import build_webhook_headers

    engine: Engine = create_engine(
        make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2")
    )
    listener: socket.socket = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port: int = listener.getsockname()[1]
    baseURL: str = f"http://127.0.0.1:{port}"
    prefix: str = str(request.param)
    jobs: dict[str, str] = {mode: f"sdk-{prefix}-{mode}" for mode in _MODES}
    archives: dict[str, bytes] = {jobId: makeArchive(jobId) for jobId in jobs.values()}
    modes: dict[str, str] = {f"doc_{jobId}": mode for mode, jobId in jobs.items()}
    nativeWrites: dict[str, int] = {}
    mutations: dict[str, int] = {}
    artifactAuthorization: list[str] = []
    delivered: list[dict[str, object]] = []
    uniqueEvents: set[str] = set()
    secret: str = "synthetic-http-contract-signing-secret"

    class QuietPublisher:
        def publish_event(self, eventId: str) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        qstash_publisher, "QStashWebhookPublisher", QuietPublisher
    )
    monkeypatch.setattr(LegacyCopyDispatch, "submit", lambda self, buildId: False)
    monkeypatch.setattr(
        JobFileStorage,
        "generate_download_url",
        lambda self, key, **options: {
            "download_url": f"{baseURL}/test/artifacts/{Path(key).stem}.zip",
            "expires_in": 3600,
        },
    )
    async def generateDownloadUrlAsync(self: JobFileStorage, key: str, **options: object) -> dict[str, object]:
        return {"download_url": f"{baseURL}/test/artifacts/{Path(key).stem}.zip", "expires_in": 3600}

    monkeypatch.setattr(JobFileStorage, "generateDownloadUrlAsync", generateDownloadUrlAsync)
    with Session(engine) as db:
        db.add(
            User(
                id="sdk-http-user",
                name="SDK HTTP contract",
                email="sdk-http@example.invalid",
            )
        )
        db.flush()
        for mode, jobId in jobs.items():
            namespace: str = f"sdk-{prefix}-{mode}"
            db.add(
                Job(
                    job_id=jobId,
                    user_id="sdk-http-user",
                    job_type="document_ingestion",
                    source_type="file",
                    status="running",
                    credits_charged=123456,
                    billing_status="charged",
                    webhook_enabled=True,
                    webhook_url=f"{baseURL}/test/webhook",
                    webhook_events=["job.parsed"]
                    if mode == "unavailable"
                    else ["job.parsed", "job.completed", "job.failed"],
                    job_metadata={
                        "document_id": f"doc_{jobId}",
                        "namespace": namespace,
                        "source_file_name": "synthetic.pdf",
                    },
                )
            )
            db.flush()
            ParsedRevisionFinalizer().finalize(
                db,
                jobId=jobId,
                resultKey=f"results/{jobId}.zip",
                checksum="synthetic-checksum",
                resultSize=len(archives[jobId]),
                chunks=[
                    {
                        "chunk_id": f"chunk-{mode}",
                        "type": "text",
                        "content": "Synthetic retained evidence",
                        "path": "synthetic.pdf/Section",
                        "metadata": {},
                    }
                ],
                storedCount=1,
                deliveryMode="url",
                sectionSummaries=None,
                documentTopSummary=None,
            )
            job: Job = _require(db.get(Job, jobId))
            attempt: IndexingAttempt = _require(
                db.get(IndexingAttempt, job.current_indexing_attempt_id)
            )
            if mode == "conflict":
                now = db.scalars(select(func.clock_timestamp())).one()
                attempt.state = "running"
                attempt.ownerToken = str(uuid4())
                attempt.executionCount = 1
                attempt.startedAt = now
                attempt.executionDeadlineAt = now + timedelta(seconds=60)
                attempt.claimExpiresAt = attempt.executionDeadlineAt
                job.indexing_status = "running"
            elif mode != "running":
                attempt.state = "failed"
                attempt.executionCount = 1
                job.status = "failed"
                job.indexing_status = "failed"
                job.failure_stage = "indexing"
                job.error_code = "UNAVAILABLE"
                job.error_message = "Synthetic initial indexing failure"
            scope: LexicalScope = LexicalScope(
                ownerId="sdk-http-user", namespace=namespace
            )
            db.add(
                LexicalNamespaceIndex(
                    id=scope.fingerprint,
                    ownerId=scope.ownerId,
                    namespace=namespace,
                    corpusKind="private",
                    indexName=f"{settings.KNOWHERE_OPENSEARCH_INDEX_PREFIX}-{scope.fingerprint[:48]}",
                    indexUuid="sdk-native-uuid",
                    state="open",
                )
            )
        db.commit()

    async def ensure(
        self: AsyncLexicalCoordination, **options: object
    ) -> tuple[str, int]:
        return ("sdk-native-uuid", 1)

    async def identity(self: AsyncOpenSearchLexicalIndex) -> str:
        return "sdk-native-uuid"

    async def write(
        self: AsyncOpenSearchLexicalIndex, *, buildId: str, units, beforeBatch=None
    ) -> None:
        documentId: str = units[0].documentId
        mode: str = modes[documentId]
        nativeWrites[mode] = nativeWrites.get(mode, 0) + 1
        if beforeBatch:
            await beforeBatch()
        if mode == "unavailable":
            raise LexicalIndexError(
                "Synthetic unavailable native boundary", isRetryable=True
            )
        if mode == "invalid":
            raise LexicalIndexError("Synthetic invalid mapping boundary")
        if mode == "timeout":
            raise TimeoutError("Synthetic execution timeout")
        if mode == "response-lost":
            await asyncio.sleep(0.15)

    monkeypatch.setattr(AsyncLexicalCoordination, "ensureIndex", ensure)
    monkeypatch.setattr(AsyncOpenSearchLexicalIndex, "getIndexUuid", identity)
    monkeypatch.setattr(AsyncOpenSearchLexicalIndex, "writeBuild", write)
    asyncEngine = create_async_engine(get_contract_database_url())
    database = AsyncLexicalDatabase(asyncEngine)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await asyncEngine.dispose()

    application: FastAPI = FastAPI(lifespan=lifespan)
    application.include_router(router, prefix="/v2/jobs")
    setup_exception_handlers(application)

    async def currentUser() -> CurrentUser:
        return CurrentUser(user_id="sdk-http-user", user_tier="tier_5")

    async def writePermission() -> None:
        return None

    async def getDatabase() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(asyncEngine, expire_on_commit=False) as db:
            yield db

    application.dependency_overrides[with_current_user] = currentUser
    application.dependency_overrides[require_write_permission] = writePermission
    application.dependency_overrides[get_db] = getDatabase

    class MetricsMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                path: str = scope["path"]
                if scope["method"] == "POST" and path.endswith("/indexing/retry"):
                    jobId: str = path.split("/")[-3]
                    mutations[jobId] = mutations.get(jobId, 0) + 1
                if path.startswith("/test/artifacts/"):
                    artifactAuthorization.extend(
                        (
                            value.decode()
                            for name, value in scope.get("headers", [])
                            if name.lower() == b"authorization"
                        )
                    )
            await self.app(scope, receive, send)

    application.add_middleware(MetricsMiddleware)

    @application.get("/test/artifacts/{jobId}.zip")
    async def artifact(jobId: str) -> Response:
        return Response(content=archives[jobId], media_type="application/zip")

    @application.post("/test/fail/{jobId}")
    async def fail(jobId: str) -> dict[str, bool]:

        def settle() -> None:
            with database.sessions.begin() as db:
                job: Job = db.scalars(
                    select(Job).where(Job.job_id == jobId).with_for_update()
                ).one()
                attempt: IndexingAttempt = db.scalars(
                    select(IndexingAttempt)
                    .where(IndexingAttempt.id == job.current_indexing_attempt_id)
                    .with_for_update()
                ).one()
                IndexingSettlement.fail(
                    db,
                    job,
                    attempt,
                    message="Synthetic indexing failure after parsed observation",
                    code="UNAVAILABLE",
                )

        await database.run(settle)
        return {"settled": True}

    @application.get("/test/metrics")
    async def metrics() -> dict[str, object]:
        return {
            "nativeWrites": nativeWrites,
            "mutations": mutations,
            "artifactAuthorization": artifactAuthorization,
            "delivered": delivered,
            "uniqueEvents": len(uniqueEvents),
        }

    @application.post("/test/webhook")
    async def webhook(request: Request) -> dict[str, bool]:
        payload = await request.json()
        timestamp, signature = request.headers["X-Knowhere-Signature"].split(",")
        signed: bytes = (
            timestamp.removeprefix("t=")
            + "."
            + json.dumps(payload, separators=(",", ":"))
        ).encode()
        expected: str = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.removeprefix("v1="), expected):
            raise HTTPException(401, "Invalid synthetic webhook signature")
        delivered.append(payload)
        uniqueEvents.add(str(payload["event_id"]))
        return {"accepted": True}

    @application.post("/test/deliver-parsed/{jobId}")
    async def deliverParsed(jobId: str) -> dict[str, object]:

        def readParsed() -> dict[str, object]:
            with database.sessions.begin() as db:
                event = db.scalars(
                    select(WebhookEvent).where(
                        WebhookEvent.job_id == jobId,
                        WebhookEvent.payload["event"].astext == "job.parsed",
                    )
                ).one()
                return QStashPayloadEnricher().enrich(db, event)

        payload = await database.run(readParsed)
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                f"{baseURL}/test/webhook",
                json=payload,
                headers=build_webhook_headers(
                    payload=payload, secret=secret, attempt_id=str(uuid4())
                ),
            )
            response.raise_for_status()
        return {
            "event": payload["event"],
            "status": payload["status"],
            "uniqueEvents": len(uniqueEvents),
        }

    @application.post("/test/deliver/{jobId}")
    async def deliver(jobId: str) -> dict[str, object]:

        def readEvents() -> list[dict[str, object]]:
            with database.sessions.begin() as db:
                return [
                    QStashPayloadEnricher().enrich(db, event)
                    for event in db.scalars(
                        select(WebhookEvent)
                        .where(WebhookEvent.job_id == jobId)
                        .order_by(WebhookEvent.created_at)
                    )
                ]

        events = await database.run(readEvents)
        byName = {str(event["event"]): event for event in events}
        ordered = [byName["job.completed"], byName["job.parsed"], byName["job.parsed"]]
        async with httpx.AsyncClient(trust_env=False) as client:
            for payload in ordered:
                response = await client.post(
                    f"{baseURL}/test/webhook",
                    json=payload,
                    headers=build_webhook_headers(
                        payload=payload, secret=secret, attempt_id=str(uuid4())
                    ),
                )
                response.raise_for_status()
        return {
            "events": events,
            "deliveredNames": [payload["event"] for payload in ordered],
            "uniqueEvents": len(uniqueEvents),
        }

    server = uvicorn.Server(
        uvicorn.Config(application, log_level="error", lifespan="on")
    )
    thread: Thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    until: float = time.monotonic() + 5
    while not server.started and thread.is_alive() and (time.monotonic() < until):
        time.sleep(0.01)
    assert server.started
    try:
        yield {"baseURL": baseURL, "jobs": jobs, "prefix": prefix}
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()
        listener.close()
        engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("KNOWHERE_NODE_SDK_ROOT")
    and (not os.environ.get("KNOWHERE_PYTHON_SDK_ROOT"))
    and (os.environ.get("KNOWHERE_SDK_HTTP_REQUIRED") != "1"),
    reason="Run explicit cross-repository SDK HTTP campaign",
)
@pytest.mark.parametrize(
    "sdkHttpRuntime", ["node", "python-sync", "python-async"], indirect=True
)
def test_standalone_sdk_against_actual_job_http_routes(
    sdkHttpRuntime: dict[str, object],
) -> None:
    prefix: str = str(sdkHttpRuntime["prefix"])
    variable: str = (
        "KNOWHERE_NODE_SDK_ROOT" if prefix == "node" else "KNOWHERE_PYTHON_SDK_ROOT"
    )
    source: str | None = os.environ.get(variable)
    if source is None:
        if os.environ.get("KNOWHERE_SDK_HTTP_REQUIRED") == "1":
            pytest.fail(f"Required SDK campaign is missing {variable}")
        pytest.skip(
            f"External SDK artifact required; run scripts/probes/verify-sdk-http-contract.py with {variable}"
        )
    environment: dict[str, str] = dict(os.environ)
    environment.update(
        {
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    if prefix == "node":
        command = [
            "node",
            str(_ROOT / "scripts/probes/sdk-http-node.mjs"),
            source,
            str(sdkHttpRuntime["baseURL"]),
            json.dumps(sdkHttpRuntime["jobs"]),
        ]
    else:
        environment["PYTHONPATH"] = os.environ.get(
            "KNOWHERE_PYTHON_SDK_IMPORT_PATH", str(Path(source) / "src")
        )
        command = [
            sys.executable,
            str(_ROOT / "scripts/probes/sdk-http-python.py"),
            prefix,
            str(sdkHttpRuntime["baseURL"]),
            json.dumps(sdkHttpRuntime["jobs"]),
        ]
    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    outcome: dict[str, object] = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome["status"] == "passed" and outcome["sdk"] == prefix
    artifactDirectory: str | None = os.environ.get("KNOWHERE_SDK_HTTP_ARTIFACT_DIR")
    if artifactDirectory:
        destination: Path = Path(artifactDirectory) / f"{prefix}.json"
        with destination.open("x") as stream:
            stream.write(json.dumps(outcome, indent=2, sort_keys=True) + "\n")


_Value = TypeVar("_Value")


def _require(value: _Value | None) -> _Value:
    assert value is not None
    return value


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)
