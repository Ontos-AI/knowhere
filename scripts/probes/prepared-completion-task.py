"""Explicit staging-only Celery include for the prepared-parser completion boundary.

Register with --include scripts.probes.prepared-completion-task on an isolated
staging worker/queue. This uses real broker delivery, parsing ownership and the
existing lifecycle transaction/post-commit inline indexing. It invokes no source
transfer, document parser, model, charging or refund entrypoint and is not evidence
for the original parse_task handler (which has a separate integration test).
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import cache
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from shared.core.celery_app import celery_app
from shared.services.retrieval.lexical.stage_observer import StageObserver

__all__: list[str] = ["completePreparedTask"]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_id: str = Field(min_length=1)
    event_id: str = Field(pattern="^[A-Za-z0-9_.:/-]{1,128}$")
    source_hash: str = Field(pattern="^[a-f0-9]{64}$")
    job_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    prepared_path: Path
    sha256: str = Field(pattern="^[a-f0-9]{64}$")
    compressed_bytes: int = Field(gt=0)
    uncompressed_bytes: int = Field(gt=0)


@cache
def _loadSupport() -> ModuleType:
    path: Path = Path(__file__).with_name("opensearch-workload.py")
    specification = importlib.util.spec_from_file_location("prepared_workload_support", path)
    if specification is None or specification.loader is None:
        raise ValueError("Prepared workload support was not installed in the worker artifact")
    module: ModuleType = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


@cache
def _getWorkerSourceHash() -> str:
    from shared.services.retrieval.lexical.transition import LexicalTransition
    return LexicalTransition.getSourceHash()


def _completePrepared(value: dict[str, object]) -> dict[str, object]:
    request: _Request = _Request.model_validate(value)
    from shared.core.config import settings
    from shared.core.database_sync import get_sync_db_context
    from shared.models.database.indexing import IndexingAttempt
    from shared.models.database.job import Job
    from shared.models.database.lexicalbuild import LexicalBuild
    from shared.services.jobs.lifecycle.service import SyncJobLifecycleService
    from shared.services.jobs.parsing import ParsingOwnership

    if (settings.ENVIRONMENT != "staging" or settings.APP_ENV != "staging"
            or os.environ.get("KNOWHERE_PREPARED_COMPLETION_CAMPAIGN") != request.campaign_id
            or _getWorkerSourceHash() != request.source_hash):
        raise ValueError("Prepared completion is enabled only for the explicitly named isolated staging campaign")
    with get_sync_db_context() as database:
        job: Job | None = database.get(Job, request.job_id)
        metadata: dict[str, object] = dict(job.job_metadata or {}) if job is not None else {}
        if (job is None or job.user_id != request.owner_id
                or metadata.get("mixed_workload_campaign") != request.campaign_id
                or metadata.get("mixed_workload_event_id") != request.event_id
                or metadata.get("mixed_workload_source_hash") != request.source_hash
                or metadata.get("prepared_sha256") != request.sha256
                or job.billing_status != "charged"):
            raise ValueError("Prepared completion requires the exact owned arrival and preseeded staging charge")
        originalCharge: tuple[str, int] = (job.billing_status, int(job.credits_charged or 0))

    def observe() -> dict[str, object]:
        with get_sync_db_context() as database:
            actual: Job | None = database.get(Job, request.job_id)
            assert actual is not None
            attempt: IndexingAttempt | None = database.get(IndexingAttempt, actual.current_indexing_attempt_id) if actual.current_indexing_attempt_id else None
            build: LexicalBuild | None = database.get(LexicalBuild, attempt.currentBuildId) if attempt and attempt.currentBuildId else None
            chargeUnchanged: bool = (actual.billing_status, int(actual.credits_charged or 0)) == originalCharge
            operation: float | None = (attempt.finishedAt - attempt.startedAt).total_seconds() * 1000 if attempt and attempt.startedAt and attempt.finishedAt else None
            return {"job_state": actual.status, "parsing_status": actual.parsing_status,
                "indexing_status": actual.indexing_status, "failure_stage": actual.failure_stage,
                "attempt_id": attempt.id if attempt else None, "attempt_origin": attempt.origin if attempt else None,
                "execution_count": attempt.executionCount if attempt else 0,
                "indexing_operation_ms": operation, "unit_count": build.unitCount if build else None,
                "indexing_started_at_utc": attempt.startedAt.astimezone(timezone.utc).isoformat() if attempt and attempt.startedAt else None,
                "indexing_finished_at_utc": attempt.finishedAt.astimezone(timezone.utc).isoformat() if attempt and attempt.finishedAt else None,
                "charge_unchanged": chargeUnchanged, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                "parsed_handoff_at_utc": actual.parsed_at.isoformat() if actual.parsed_at else None,
                "search_ready_at_utc": actual.completed_at.isoformat() if actual.status == "done" and actual.completed_at else None}

    def finalize(options: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], SyncJobLifecycleService().finalize_job_success(
            job_id=str(options["job_id"]), result_s3_key=str(options["result_s3_key"]),
            checksum=str(options["checksum"]), zip_size=int(str(options["zip_size"])),
            chunks=cast(list[dict[str, object]], options["chunks"]),
            stored_count=int(str(options["stored_count"])), delivery_mode="url",
            section_summaries=cast(dict[str, str] | None, options["section_summaries"]),
            document_top_summary=cast(str | None, options["document_top_summary"]),
            parsingOwnerToken=str(options["parsingOwnerToken"])))

    support: ModuleType = _loadSupport()
    result: dict[str, object] = support.WorkloadSupport.completePrepared(jobId=request.job_id,
        path=request.prepared_path, expectedSha256=request.sha256, compressedBytes=request.compressed_bytes,
        uncompressedBytes=request.uncompressed_bytes, claim=ParsingOwnership().claim, finalize=finalize, observe=observe)
    if result.get("charge_unchanged") is not True or int(str(result.get("execution_count", 0))) > 1:
        result["status"] = "failed"
        result["boundary_contract_failed"] = True
    return result


@celery_app.task(name="knowhere.probes.complete_prepared", acks_late=True, max_retries=0, ignore_result=False)
def completePreparedTask(value: dict[str, object]) -> dict[str, object]:
    request: _Request = _Request.model_validate(value)
    with StageObserver.bindEvent(request.campaign_id, request.event_id, kind="write"):
        with StageObserver.measure("inline_indexing_ms"):
            result: dict[str, object] = _completePrepared(value)
        operation: object = result.get("indexing_operation_ms")
        if isinstance(operation, (int, float)):
            StageObserver.record("indexing_operation_ms", operation)
        started, finished = result.get("indexing_started_at_utc"), result.get("indexing_finished_at_utc")
        if isinstance(started, str) and isinstance(finished, str):
            StageObserver.recordInterval("indexing_intervals_utc", started, finished)
        return result
