"""Opt-in original parse task across real Redis, Celery and gevent boundaries."""

from __future__ import annotations

import asyncio
import json
from io import BytesIO
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4

from celery import Celery
import httpx
import pytest
import redis
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from shared.testing.contract_runtime import (
    PostgreSQLProcess, clear_application_modules, configure_contract_environment, configure_runtime_database_role,
    get_contract_database_url, prepare_contract_storage,
)


def _allocatePort() -> int:
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return int(connection.getsockname()[1])


def _stopOwnedBrokerContainer(name: str) -> None:
    docker: list[str] = ["sudo", "-n", "docker"]
    identities: list[str] = subprocess.check_output(
        [*docker, "ps", "-aq", "--no-trunc", "--filter", f"name=^/{name}$",
         "--filter", "label=codex.task=01a1ac05-72d0-72b1-923f-30e678f290b1"],
        text=True, timeout=10,
    ).splitlines()
    if identities:
        identity, = identities
        subprocess.run([*docker, "stop", "--time=3", identity],
            capture_output=True, timeout=10, check=True)


def _stopBroker(broker: subprocess.Popen[bytes], dockerName: str | None) -> None:
    try:
        if dockerName is not None:
            _stopOwnedBrokerContainer(dockerName)
            try:
                broker.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                pass
        if broker.poll() is None:
            broker.terminate()
        try:
            broker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            broker.kill()
            broker.wait(timeout=5)
    finally:
        # Docker may finish creating a container just as its launcher exits.
        if dockerName is not None:
            _stopOwnedBrokerContainer(dockerName)


def test_original_parse_task_indexes_inline_and_never_reparses_after_index_failure(
    monkeypatch: pytest.MonkeyPatch, postgresql_proc: PostgreSQLProcess,
) -> None:
    endpoint: str | None = os.environ.get("KNOWHERE_TEST_OPENSEARCH_URL")
    if os.environ.get("KNOWHERE_TEST_REAL_WORKER") != "1" or not endpoint:
        pytest.skip("Set KNOWHERE_TEST_REAL_WORKER=1 with an isolated native node and real Redis executable")
    configure_contract_environment(monkeypatch, postgresql_proc)
    monkeypatch.setenv("KNOWHERE_LEXICAL_BACKEND", "opensearch")
    monkeypatch.setenv("KNOWHERE_OPENSEARCH_URL", endpoint)
    monkeypatch.setenv("BILLING_ENABLED", "true")
    clear_application_modules()
    asyncio.run(prepare_contract_storage())
    asyncio.run(configure_runtime_database_role(monkeypatch, get_contract_database_url(postgresql_proc)))
    clear_application_modules()
    # Contract setup uses fakeredis for ordinary API tests. This campaign must
    # restore the actual redis-py transport before constructing either producer
    # or observer; the separate worker already uses an unpatched interpreter.
    from redis.client import Redis as RealRedis
    monkeypatch.setattr(redis, "Redis", RealRedis)
    monkeypatch.setattr(redis, "from_url", RealRedis.from_url)
    from kombu.transport.redis import PrefixedStrictRedis
    assert not any(base.__module__.startswith("fakeredis") for base in PrefixedStrictRedis.__mro__), (
        "Run the real-broker campaign in its own fresh isolated pytest process")
    from shared.core.config import settings
    from shared.core.config.storage import get_cached_storage_adapter
    from shared.models.database.document import Document
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.job_result import JobResult
    from shared.models.database.user import User

    probe: Path = Path(os.environ.get("KNOWHERE_TEST_WORKER_OUTPUT", str(Path(settings.TMP_PATH) / "original-worker-probe")))
    probe.mkdir(parents=True, exist_ok=True)
    repository: Path = Path(__file__).resolve().parents[4]
    bootstrap: Path = repository / "scripts/probes/original-parse-worker.py"
    assert bootstrap.is_file(), "The prepared parser seam must be implemented before running the broker campaign"
    suffix: str = uuid4().hex[:12]
    indexPrefix: str = f"knowhere-kw01-worker-{suffix}"
    queue: str = f"kw02-{suffix}"
    jobs: tuple[str, ...] = tuple(f"kw02-{suffix}-{kind}" for kind in ("success", "failure", "timeout"))
    engine = create_engine(make_url(get_contract_database_url()).set(drivername="postgresql+psycopg2"))
    with Session(engine) as session:
        session.add(User(id="real-worker-user", name="Real worker contract", email="worker@example.invalid"))
        session.flush()
        for jobId in jobs:
            key: str = f"uploads/{jobId}.txt"
            get_cached_storage_adapter().upload_fileobj(BytesIO(f"Prepared original source {jobId}".encode()), key, bucket=settings.S3_BUCKET_NAME)
            session.add(Job(job_id=jobId, user_id="real-worker-user", job_type="document_ingestion",
                source_type="file", status="pending", s3_key=key, billing_status="charged", credits_charged=123456, page_count=1,
                job_metadata={"document_id": f"doc_{jobId}", "namespace": queue, "source_file_name": f"{jobId}.txt",
                    "parsing_params": {"summary_use_llm": False, "top_summary_use_llm": False}}))
            session.add(Document(document_id=f"doc_{jobId}", user_id="real-worker-user", namespace=queue,
                source_file_name=f"{jobId}.txt", status="active", accepted_job_id=jobId))
        session.commit()
    port: int = _allocatePort()
    redisExecutable: str = os.environ.get("KNOWHERE_TEST_REDIS_EXECUTABLE") or shutil.which("redis-server") or "redis-server"
    dockerBrokerName: str | None = os.environ.get("KNOWHERE_TEST_REDIS_CONTAINER_NAME")
    if dockerBrokerName is not None and dockerBrokerName != "knowhere-kw01-original-redis":
        raise ValueError("The Oracle broker cleanup must target its exact owned probe container")
    log = (probe / "worker.log").open("w")
    brokerLog = (probe / "redis.log").open("w")
    broker = subprocess.Popen([redisExecutable, "--bind", "127.0.0.1", "--port", str(port),
        "--save", "", "--appendonly", "no", "--maxmemory", "32mb", "--maxmemory-policy", "noeviction"], stdout=brokerLog, stderr=subprocess.STDOUT)
    environment: dict[str, str] = dict(os.environ)
    environment.update({"REDIS_HOST": "127.0.0.1", "REDIS_PORT": str(port), "REDIS_DATABASE": "0",
        "CELERY_REDIS_URL": f"redis://127.0.0.1:{port}/0", "KNOWHERE_PROBE_ORIGINAL_PARSE": "1",
        "KNOWHERE_PROBE_OUTPUT_ROOT": str(probe), "LOGFIRE_IGNORE_NO_CONFIG": "1", "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": os.pathsep.join(str(path) for path in (repository / "apps/worker", repository / "packages/shared-python", repository / "scripts/probes")),
        "KNOWHERE_OPENSEARCH_INDEX_PREFIX": indexPrefix, "WORKER_CONCURRENCY": "2"})
    worker: subprocess.Popen[str] | None = None
    dockerIdentity: str | None = None
    client: redis.Redis = redis.Redis(host="127.0.0.1", port=port, socket_timeout=1)
    celery: Celery = Celery("original-worker-probe", broker=f"redis://127.0.0.1:{port}/0")
    celery.conf.update(broker_transport_options={"global_keyprefix": "{celery}"},
        task_queue_max_priority=10, task_default_priority=5)
    started: float = time.monotonic()
    try:
        brokerReadyBy: float = time.monotonic() + 30
        while time.monotonic() < brokerReadyBy:
            if broker.poll() is not None:
                pytest.fail("Owned Redis launcher exited before readiness")
            try:
                if client.ping():
                    break
            except redis.RedisError:
                time.sleep(0.05)
        else:
            pytest.fail("Owned Redis did not become ready within the setup budget")
        brokerReadySeconds: float = time.monotonic() - started
        arguments: list[str] = ["-m", "celery", "-A", "original-parse-worker:celery_app", "worker", "--pool=gevent",
            "--concurrency=2", "--prefetch-multiplier=1", "-Q", queue, "--without-gossip", "--without-mingle", "--without-heartbeat", "--loglevel=INFO"]
        image: str | None = os.environ.get("KNOWHERE_TEST_WORKER_IMAGE")
        if image:
            # The reviewed outer Oracle unit/guard bounds all owned processes.
            environmentFile: Path = probe / "worker.env"
            allowed: frozenset[str] = frozenset({"DATABASE_URL", "DB_SSL_MODE", "DB_USE_NULL_POOL", "REDIS_HOST", "REDIS_PORT", "REDIS_DATABASE",
                "CELERY_REDIS_URL", "S3_TYPE", "S3_BUCKET_NAME", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_TEMP_PATH", "S3_ENDPOINT_URL",
                "S3_PRIVATE_DOMAIN", "S3_RESULTS_BUCKET", "S3_REGION", "S3_USE_SSL", "S3_ADDRESSING_STYLE", "OBJECT_STORAGE_LOCAL_ROOT", "TMP_PATH",
                "ENVIRONMENT", "API_STANDALONE_MODE_ENABLED", "LOGFIRE_IGNORE_NO_CONFIG", "OTEL_SDK_DISABLED", "LOGFIRE_SEND_TO_LOGFIRE",
                "TELEMETRY_ENABLED", "PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "WEBHOOK_MASTER_KEY", "DS_KEY", "DS_URL",
                "BILLING_ENABLED", "WORKER_CONCURRENCY", "KNOWHERE_LEXICAL_BACKEND", "KNOWHERE_OPENSEARCH_URL", "KNOWHERE_OPENSEARCH_INDEX_PREFIX",
                "KNOWHERE_INDEXING_TIMEOUT_SECONDS", "KNOWHERE_INDEXING_SETTLEMENT_TIMEOUT_SECONDS", "KNOWHERE_PROBE_ORIGINAL_PARSE", "KNOWHERE_PROBE_OUTPUT_ROOT",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "AWS_EC2_METADATA_DISABLED",
                "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"})
            environmentFile.write_text("\n".join(f"{key}={value}" for key, value in environment.items()
                if key in allowed and "\n" not in value and "\r" not in value) + "\n")
            environmentFile.chmod(0o600)
            ownedRoot: Path = Path(os.environ["KNOWHERE_TEST_OWNED_ROOT"])
            command: list[str] = ["sudo", "-n", "docker", "run", "--pull=never", "--detach", "--name", "knowhere-kw01-original-worker",
                "--label", "codex.task=01a1ac05-72d0-72b1-923f-30e678f290b1", "--network=host", "--user", f"{os.getuid()}:{os.getgid()}",
                "--cpus=0.15", "--memory=768m", "--pids-limit=96", "--read-only", "--no-healthcheck", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--tmpfs=/tmp:rw,size=64m,mode=1777", "--device-read-bps=/dev/sda:2mb", "--device-write-bps=/dev/sda:1mb",
                "--device-read-iops=/dev/sda:50", "--device-write-iops=/dev/sda:25", "--log-opt=max-size=5m", "--log-opt=max-file=2",
                "--env-file", str(environmentFile), "--volume", f"{ownedRoot}:{ownedRoot}", "--workdir", str(repository / "apps/worker"),
                "--entrypoint", "python", image, *arguments]
            dockerIdentity = subprocess.check_output(command, text=True).strip()
        else:
            worker = subprocess.Popen([sys.executable, *arguments], env=environment, stdout=log, stderr=subprocess.STDOUT, text=True)
        for jobId in jobs:
            celery.send_task("app.core.tasks.document_ingestion_tasks.parse_task", args=[jobId], kwargs={"user_id": "real-worker-user"}, queue=queue, retry=False)
        deadline: float = time.monotonic() + 180
        while time.monotonic() < deadline:
            with Session(engine) as session:
                states: list[str] = list(session.scalars(select(Job.status).where(Job.job_id.in_(jobs))))
            if len(states) == 3 and all(state in ("done", "failed") for state in states):
                break
            if worker is not None and worker.poll() is not None:
                pytest.fail("Original worker exited before durable settlement: " + (probe / "worker.log").read_text()[-8000:])
            time.sleep(0.2)
        else:
            with Session(engine) as session:
                diagnostic: list[dict[str, object]] = [{"job_id": job.job_id, "status": job.status,
                    "parsing_status": job.parsing_status, "indexing_status": job.indexing_status,
                    "error_code": job.error_code} for job in session.scalars(select(Job).where(Job.job_id.in_(jobs)))]
            (probe / "unsettled-jobs.json").write_text(json.dumps(diagnostic, indent=2) + "\n")
            output: str = subprocess.check_output(["sudo", "-n", "docker", "logs", "--tail=100", dockerIdentity], text=True,
                stderr=subprocess.STDOUT) if dockerIdentity else (probe / "worker.log").read_text()
            pytest.fail("Original parse tasks did not settle within the bounded integration campaign: " + output[-10000:])
        with Session(engine) as session:
            originalResults: dict[str, str] = {}
            for jobId in jobs:
                job = session.get(Job, jobId)
                attempt = session.scalars(select(IndexingAttempt).where(IndexingAttempt.jobId == jobId)).one()
                result = session.scalars(select(JobResult).where(JobResult.job_id == jobId)).one()
                originalResults[jobId] = result.id
                assert job is not None and job.parsing_status == "completed"
                assert job.status == ("done" if jobId.endswith("success") else "failed")
                assert job.billing_status == "charged" and job.credits_charged == 123456
                assert attempt.executionCount == 1
                assert result.result_s3_key and (get_cached_storage_adapter().get_object_size(result.result_s3_key, bucket=settings.S3_RESULTS_BUCKET) or 0) > 0
                if jobId.endswith("timeout"):
                    assert job.error_code == "DEADLINE_EXCEEDED"
        redeliveries: set[str] = {celery.send_task("app.core.tasks.document_ingestion_tasks.parse_task", args=[jobId],
            kwargs={"user_id": "real-worker-user"}, queue=queue, retry=False).id for jobId in jobs}
        marker: str = celery.send_task("knowhere.probe.completed", queue=queue, retry=False).id
        deadline = time.monotonic() + 30
        events: list[dict[str, object]] = []
        finished: set[str] = set()
        while time.monotonic() < deadline:
            events = [json.loads(line) for line in (probe / "events.jsonl").read_text().splitlines()]
            finished = {str(event["task_id"]) for event in events if event["event"] == "task_finished"}
            if redeliveries.issubset(finished) and client.exists(f"probe:{marker}"):
                break
            time.sleep(0.1)
        assert redeliveries.issubset(finished), "Every duplicate delivery must finish before checking parser and indexing counts"
        assert client.exists(f"probe:{marker}")
        assert sorted(str(event["job_id"]) for event in events if event["event"] == "parser") == sorted(jobs)
        assert all(event["event"] != "indexing_queue" for event in events)
        receipt: dict[str, object] = {"completed": True, "jobs": jobs, "results": originalResults,
            "elapsed_seconds": time.monotonic() - started, "broker_setup_seconds": brokerReadySeconds,
            "transport": "real Redis/Celery/gevent", "parser": "prepared output seam",
            "timeout_seconds": settings.KNOWHERE_INDEXING_TIMEOUT_SECONDS, "concurrency": 2, "prefetch_multiplier": 1}
        (probe / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    finally:
        try:
            if dockerIdentity:
                try:
                    with log:
                        subprocess.run(["sudo", "-n", "docker", "logs", dockerIdentity],
                            stdout=log, stderr=subprocess.STDOUT, timeout=10)
                finally:
                    subprocess.run(["sudo", "-n", "docker", "stop", "--time=3", dockerIdentity], capture_output=True, timeout=10)
                    subprocess.run(["sudo", "-n", "docker", "rm", dockerIdentity], capture_output=True, timeout=10)
            if worker is not None:
                worker.terminate()
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=5)
        finally:
            try:
                client.close()
                celery.close()
            finally:
                try:
                    _stopBroker(broker, dockerBrokerName)
                finally:
                    log.close()
                    brokerLog.close()
                    engine.dispose()
                    with httpx.Client(base_url=endpoint, trust_env=False) as native:
                        for row in native.get(f"/_cat/indices/{indexPrefix}-*?format=json").json():
                            native.delete("/" + row["index"])
