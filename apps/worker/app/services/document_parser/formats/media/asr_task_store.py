from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select

from shared.core.database_sync import get_sync_db_context
from shared.models.database.job import Job

# Job metadata key holding the submitted Fun-ASR task, so a redelivered parse
# task resumes polling the billed task instead of submitting a second one.
ASR_TASK_METADATA_KEY = "media_asr_task"


@dataclass(frozen=True)
class AsrTaskRef:
    task_id: str
    token_id: str


class AsrTaskStore(Protocol):
    def load(self, job_id: str) -> AsrTaskRef | None: ...

    def save(self, job_id: str, ref: AsrTaskRef) -> None: ...


class JobMetadataAsrTaskStore:
    """Persist the submitted ASR task on the durable Job row."""

    def load(self, job_id: str) -> AsrTaskRef | None:
        with get_sync_db_context() as db:
            job_metadata = db.execute(
                select(Job.job_metadata).where(Job.job_id == job_id)
            ).scalar_one_or_none()
        if not isinstance(job_metadata, dict):
            return None
        raw_ref = job_metadata.get(ASR_TASK_METADATA_KEY)
        if not isinstance(raw_ref, dict):
            return None
        task_id = raw_ref.get("task_id")
        token_id = raw_ref.get("token_id")
        if not isinstance(task_id, str) or not task_id:
            return None
        if not isinstance(token_id, str) or not token_id:
            return None
        return AsrTaskRef(task_id=task_id, token_id=token_id)

    def save(self, job_id: str, ref: AsrTaskRef) -> None:
        with get_sync_db_context() as db:
            job = db.execute(
                select(Job).where(Job.job_id == job_id).with_for_update()
            ).scalar_one_or_none()
            if job is None:
                return
            job.job_metadata = {
                **dict(job.job_metadata or {}),
                ASR_TASK_METADATA_KEY: {
                    "task_id": ref.task_id,
                    "token_id": ref.token_id,
                },
            }
