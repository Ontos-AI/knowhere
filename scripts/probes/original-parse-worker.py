"""Opt-in worker bootstrap: original parse task with prepared parser output only.

Run through the bounded real-broker contract, never through a production worker.
Source download, ownership, page billing, packaging, parsed commit and inline
indexing remain the application implementations. No model/provider is called.
"""

# ruff: noqa: E402 -- patch drivers and cap native threads before application imports
from __future__ import annotations

import json
import os
from pathlib import Path
import time

if os.environ.get("KNOWHERE_PROBE_ORIGINAL_PARSE") != "1":
    raise RuntimeError("Original parse probe requires an explicit isolated worker environment")

from gevent.monkey import is_module_patched
if not is_module_patched("socket") or not is_module_patched("threading"):
    raise RuntimeError("Original parse probe must start with Celery --pool=gevent")
from psycogreen.gevent import patch_psycopg
patch_psycopg()
from app.core.runtime_limits import apply_native_thread_caps
apply_native_thread_caps()

from celery import Task
from celery.signals import before_task_publish, task_postrun
import gevent
import pandas as pd
import redis
from sqlalchemy import func, select

from shared.core.celery_app import celery_app
from shared.core.database_sync import get_sync_db_context
from shared.models.database.job import Job
from shared.services.jobs.parsing import ParsingOwnership
from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.worker import IndexingWorker, _Claim
from shared.services.retrieval.lexical.input import LexicalInput
from shared.services.retrieval.lexical.opensearch import OpenSearchLexicalIndex
from app.core.tasks import document_ingestion_tasks
import app.core.worker_bootstrap  # noqa: F401 -- register the real worker lifecycle signals
from app.services.document_ingestion import parse_execution
from app.services.document_parser.orchestration.parse_output import ParseOutput

_ROOT: Path = Path(os.environ["KNOWHERE_PROBE_OUTPUT_ROOT"]).resolve()
_WRITE = IndexingWorker._writeBuild


def _record(event: str, *, jobId: str = "", taskId: str = "") -> None:
    with (_ROOT / "events.jsonl").open("a") as stream:
        stream.write(json.dumps({"event": event, "job_id": jobId, "task_id": taskId,
            "monotonic": time.monotonic()}) + "\n")


def _prepareParser(*, job_id: str, output_dir: str, filename: str, **options: object) -> ParseOutput:
    if not job_id.startswith("kw02-"):
        raise RuntimeError("Prepared parser refuses a job outside its owned probe scope")
    with get_sync_db_context() as session:
        job: Job = session.scalars(select(Job).where(Job.job_id == job_id)).one()
        if job.parsing_owner_token is None:
            raise RuntimeError("Original parse task reached provider work without durable ownership")
        ParsingOwnership.assertJobOwned(job, job.parsing_owner_token, session.scalars(select(func.clock_timestamp())).one())
        if job.billing_status != "charged" or job.credits_charged != 123456:
            raise RuntimeError("Prepared parser observed unexpected billing mutation")
    _record("parser", jobId=job_id)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    frame: pd.DataFrame = pd.DataFrame([{"know_id": f"{job_id}-chunk", "type": "text",
        "content": f"Engineering acceptance evidence {job_id}", "path": f"{filename}/Engineering acceptance",
        "summary": "Prepared engineering evidence", "length": 80}])
    return ParseOutput(output_dir=output_dir, parsed_df=frame)


def _writeBuild(self: IndexingWorker, native: OpenSearchLexicalIndex, claim: _Claim, payload: LexicalInput) -> None:
    _record("indexing_inline", jobId=claim.context.jobId)
    if claim.context.jobId.endswith("failure"):
        raise LexicalIndexError("Injected native unavailability at original task boundary", isRetryable=True)
    if claim.context.jobId.endswith("timeout"):
        gevent.sleep(60)
        raise AssertionError("Forward indexing continued after its fixed deadline")
    _WRITE(self, native, claim, payload)


@celery_app.task(name="knowhere.probe.completed", bind=True)
def _completeProbe(task: Task) -> None:
    identity: str = str(task.request.id)
    with redis.Redis.from_url(os.environ["CELERY_REDIS_URL"]) as broker:
        broker.set(f"probe:{identity}", "completed", ex=60)


@before_task_publish.connect
def _recordPublication(sender: str | None = None, **options: object) -> None:
    if sender == "shared.services.retrieval.lexical.tasks.runIndexing":
        _record("indexing_queue")


@task_postrun.connect
def _recordTask(task_id: str | None = None, args: tuple[object, ...] | list[object] | None = None, **options: object) -> None:
    _record("task_finished", taskId=task_id or "", jobId=str(args[0]) if args else "")


parse_execution.parse_service.checkerboard_parse_output = _prepareParser
IndexingWorker._writeBuild = _writeBuild
assert document_ingestion_tasks.celery_app is celery_app
assert "app.core.tasks.document_ingestion_tasks.parse_task" in celery_app.tasks
